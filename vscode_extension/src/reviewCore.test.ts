// The panel review's pure logic (reviewCore.ts). The pending record below and
// its digest are the Python side's: `fixtures/review/pending.json` is the file
// `panel_review.pending_record` and `record_json` write, which
// `packages/mcp/tests/test_panel_review.py::TestTheSharedVectors` asserts byte for
// byte, and `fixtures/review/decision.json` is this side's `decisionRecord` of
// it, which the same Python test feeds through `read_decision`. A key added,
// renamed or reordered on either side therefore fails a suite on both.

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import {
  ALREADY_ANSWERED,
  answerPlan,
  answersRecord,
  CHANGE_SET_ID_PATTERN,
  checkApproval,
  clockTime,
  compareRecords,
  type DecisionFs,
  decisionFileName,
  decisionRecord,
  EARLIER_ANSWER_IN_PLACE,
  type EntryFs,
  escapeHtml,
  intentLabel,
  isLive,
  isPendingFileName,
  listedReviewKeys,
  metaItems,
  NO_INTENT,
  NO_LONGER_WAITING,
  newNonce,
  type PendingRecord,
  parsePending,
  parseTabAnswer,
  pendingFileName,
  REVIEW_HEADER,
  REVIEW_SCRIPT,
  type ReviewEntry,
  readReviewEntry,
  reviewBadgeTooltip,
  reviewCsp,
  reviewHtml,
  reviewIdentity,
  reviewKey,
  reviewNodes,
  reviewsDir,
  reviewTabTitle,
  reviewTransitions,
  STALE_MS,
  samePath,
  sameReview,
  textDigest,
  textHtml,
  WAITING_LINE,
  warningSpan,
  windowReviews,
  writeDecision,
} from "./reviewCore";
import { toolchainHome } from "./toolchain";

// --- The Python-written record ----------------------------------------------

const PYTHON_TEXT =
  "\nChange set cs-20261025T120000Z-0a1b2c on «Vehicle»:\n  set Größe → 42 µs\n  日本 𝄞\n";
const PYTHON_DIGEST = "sha256:cf4b337e345e8825ee109541573f44058f14904733dc2397c4c24d51dc88bfc9";
const PYTHON_WAIT = "5f1e0c9a7b3d4e2f8a6b1c0d9e8f7a6b";
const OTHER_WAIT = "0f".repeat(16);

const VECTOR_DIR = path.join(__dirname, "..", "fixtures", "review");
const PYTHON_RECORD = fs.readFileSync(path.join(VECTOR_DIR, "pending.json"), "utf8");
const DECISION_VECTOR = fs.readFileSync(path.join(VECTOR_DIR, "decision.json"), "utf8");
const WARNING_RECORD = fs.readFileSync(path.join(VECTOR_DIR, "pending-warning.json"), "utf8");

/** The vector record's `created`; an entry's mtime is a heartbeat taken relative to it. */
const CREATED_MS = Date.parse("2026-10-26T12:00:00Z");
/** An instant half an hour into the wait. */
const NOW_MS = CREATED_MS + 30 * 60_000;

function record(overrides: Partial<PendingRecord> = {}): PendingRecord {
  const parsed = parsePending(PYTHON_RECORD);
  assert.ok(parsed.ok);
  return { ...parsed.record, ...overrides };
}

function withText(text: string, overrides: Partial<PendingRecord> = {}): PendingRecord {
  return record({ text, digest: textDigest(text), ...overrides });
}

// --- parsePending -----------------------------------------------------------

test("parsePending: the record panel_review wrote is read field for field", () => {
  const parsed = parsePending(PYTHON_RECORD);
  assert.ok(parsed.ok);
  assert.equal(parsed.record.change_set_id, "cs-20261025T120000Z-0a1b2c");
  assert.equal(parsed.record.question, "delta");
  assert.equal(parsed.record.wait, PYTHON_WAIT);
  assert.deepEqual(parsed.record.project, {
    name: "Vehicle Control",
    manifest: "/Users/dev/p/swcraft.yaml",
  });
  assert.equal(parsed.record.client, "Codex");
  assert.equal(parsed.record.intent, "Add the wheel speed port");
  assert.equal(parsed.record.text, PYTHON_TEXT);
  assert.equal(parsed.record.digest, PYTHON_DIGEST);
});

test("parsePending: a null intent, and a null client, is a record", () => {
  const value = JSON.parse(PYTHON_RECORD);
  value.intent = null;
  value.client = null;
  const parsed = parsePending(JSON.stringify(value));
  assert.ok(parsed.ok);
  assert.equal(parsed.record.intent, null);
  assert.equal(parsed.record.client, null);
});

for (const key of [
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
]) {
  test(`parsePending: a record without ${key} is refused`, () => {
    const value = JSON.parse(PYTHON_RECORD);
    delete value[key];
    assert.deepEqual(parsePending(JSON.stringify(value)), { ok: false, reason: `no ${key}` });
  });
}

for (const format of [1, 3, 0, "2", true, null]) {
  test(`parsePending: format ${JSON.stringify(format)} is an unknown format`, () => {
    const value = JSON.parse(PYTHON_RECORD);
    value.format = format;
    assert.deepEqual(parsePending(JSON.stringify(value)), {
      ok: false,
      reason: "an unknown format",
    });
  });
}

test("parsePending: a record still carrying deadline is refused", () => {
  const value = { ...JSON.parse(PYTHON_RECORD), deadline: "2026-10-26T12:10:00Z" };
  assert.deepEqual(parsePending(JSON.stringify(value)), { ok: false, reason: "an unknown key" });
});

test("parsePending: a format 1 record, deadline and all, is refused", () => {
  const value = { ...JSON.parse(PYTHON_RECORD), format: 1, deadline: "2026-10-26T12:10:00Z" };
  assert.equal(parsePending(JSON.stringify(value)).ok, false);
});

test("parsePending: a file that is not JSON, or not an object, is refused", () => {
  assert.deepEqual(parsePending("{ not json"), { ok: false, reason: "not JSON" });
  assert.deepEqual(parsePending(""), { ok: false, reason: "not JSON" });
  assert.deepEqual(parsePending("[1]"), { ok: false, reason: "not a JSON object" });
  assert.deepEqual(parsePending("null"), { ok: false, reason: "not a JSON object" });
});

test("parsePending: an extra key, a bad id, question, project or digest is refused", () => {
  const cases: [Record<string, unknown>, string][] = [
    [{ extra: 1 }, "an unknown key"],
    [{ change_set_id: "../../etc/passwd" }, "an invalid change_set_id"],
    [{ change_set_id: "cs-20261025T120000Z-0A1B2C" }, "an invalid change_set_id"],
    [{ question: "other" }, "an unknown question"],
    [{ wait: null }, "an invalid wait"],
    [{ wait: "0F".repeat(16) }, "an invalid wait"],
    [{ wait: "0f".repeat(15) }, "an invalid wait"],
    [{ project: { name: "x" } }, "an invalid project"],
    [{ project: { name: 1, manifest: "/p" } }, "an invalid project"],
    [{ project: { name: "x", manifest: "/p", more: 1 } }, "an invalid project"],
    [{ client: 3 }, "an invalid client"],
    [{ client: { name: "Codex" } }, "an invalid client"],
    [{ intent: 3 }, "an invalid intent"],
    [{ text: null }, "an invalid text"],
    [{ digest: "sha256:XYZ" }, "an invalid digest"],
    [{ created: "2026-10-26 12:00:00" }, "an invalid created"],
    [{ created: 0 }, "an invalid created"],
  ];
  for (const [patch, reason] of cases) {
    const value = { ...JSON.parse(PYTHON_RECORD), ...patch };
    assert.deepEqual(parsePending(JSON.stringify(value)), { ok: false, reason }, reason);
  }
});

test("CHANGE_SET_ID_PATTERN: the engine's pattern", () => {
  assert.ok(CHANGE_SET_ID_PATTERN.test("cs-20261025T120000Z-0a1b2c"));
  assert.ok(!CHANGE_SET_ID_PATTERN.test("cs-20261025T120000Z-0a1b2c.delta"));
});

test("the shared vectors: both pending records are format 2, without deadline, and parse", () => {
  for (const text of [PYTHON_RECORD, WARNING_RECORD]) {
    const value = JSON.parse(text);
    assert.equal(value.format, 2);
    assert.ok(!Object.hasOwn(value, "deadline"));
    assert.ok(parsePending(text).ok);
  }
  assert.equal(JSON.parse(DECISION_VECTOR).format, 2);
});

test("the shared vectors: STALE_MS is panel_review.STALE_SECONDS, the same literal on both sides", () => {
  // packages/mcp/tests/test_panel_review.py asserts STALE_SECONDS == 60.
  assert.equal(STALE_MS, 60_000);
});

test("the shared vectors: this side's decision on the Python record is the decision file", () => {
  assert.equal(decisionRecord(record(), "approve", PYTHON_DIGEST), DECISION_VECTOR);
});

test("decisionRecord: the decision carries the record's own wait token", () => {
  const decision = JSON.parse(
    decisionRecord(record({ wait: OTHER_WAIT }), "reject", PYTHON_DIGEST),
  );
  assert.equal(decision.wait, OTHER_WAIT);
});

// --- samePath, isLive -------------------------------------------------------

test("samePath: POSIX compares exactly", () => {
  assert.ok(samePath("/Users/x/p/swcraft.yaml", "/Users/x/p/swcraft.yaml", "posix"));
  assert.ok(!samePath("/Users/x/p/swcraft.yaml", "/users/x/p/swcraft.yaml", "posix"));
});

test("samePath: Windows matches the server's as_posix() against realpathSync.native", () => {
  assert.ok(samePath("C:/Users/X/p/swcraft.yaml", "c:\\users\\x\\p\\swcraft.yaml", "windows"));
  assert.ok(samePath("C:/Users/X/p/swcraft.yaml", "C:\\Users\\X\\p\\swcraft.yaml", "windows"));
});

test("samePath: another project is no match", () => {
  assert.ok(!samePath("C:/Users/X/p/swcraft.yaml", "C:\\Users\\X\\q\\swcraft.yaml", "windows"));
  assert.ok(!samePath("/Users/dev/p/swcraft.yaml", "/Users/dev/q/swcraft.yaml", "posix"));
});

test("isLive: a heartbeat younger than STALE_MS, never one at or past it", () => {
  assert.ok(isLive(NOW_MS, NOW_MS));
  assert.ok(isLive(NOW_MS - (STALE_MS - 1), NOW_MS));
  assert.ok(!isLive(NOW_MS - STALE_MS, NOW_MS));
  assert.ok(!isLive(NOW_MS - 6 * 3_600_000, NOW_MS));
  // A day into the wait, a fresh heartbeat is live: no clock bound ends it.
  assert.ok(isLive(CREATED_MS + 86_400_000 - 1_000, CREATED_MS + 86_400_000));
});

// --- The tab's page --------------------------------------------------------

/** The text the server renders for a delta (`review.py::render_delta_review`'s shape). */
const DELTA_TEXT = [
  "Review this change set before it is committed.",
  "",
  "Intent: Add a brake temperature signal",
  "",
  "The exact delta (3 entries):",
  "  1. create interfaces/If_BrakeTemperature — sender-receiver, one data element",
  "       after: {kind: sender_receiver}",
  "  2. update swc/BrakeController — one provide port added",
  "       before: {ports: 6}",
  "       after: {ports: 7}",
  "  10. remove connectors/Conn_Old",
  "       before (summary): {}",
  "  created by update remove",
  "",
  "Nothing has been applied yet and the model is byte-identical. Commit exactly this delta?",
].join("\n");

/** The `<pre>`'s inner HTML in *html*. */
function preInner(html: string): string {
  const open = '<pre id="text">\n';
  const start = html.indexOf(open) + open.length;
  return html.slice(start, html.indexOf("</pre>", start));
}

/** What a browser's `textContent` gives for *inner*: tags stripped, the five entities unescaped. */
function textContentOf(inner: string): string {
  return inner
    .replace(/<[^>]*>/g, "")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&");
}

test("reviewTabTitle: 'Review: <intent>', flattened, or '(no intent given)'", () => {
  assert.equal(reviewTabTitle(record()), "Review: Add the wheel speed port");
  assert.equal(reviewTabTitle(record({ intent: null })), "Review: (no intent given)");
  assert.equal(reviewTabTitle(record({ intent: "" })), "Review: (no intent given)");
  assert.equal(reviewTabTitle(record({ intent: "a\nb\u2028c" })), "Review: a b c");
  const html = reviewHtml(record({ intent: "<x> & y" }), { nonce: "abc123", utcOffsetMinutes: 0 });
  assert.ok(html.includes("<title>Review: &lt;x&gt; &amp; y</title>"));
});

test("reviewHtml: the header and the meta line with a client", () => {
  const html = reviewHtml(record(), { nonce: "abc123", utcOffsetMinutes: 0 });
  assert.ok(html.includes(`<h1>${REVIEW_HEADER}</h1>`));
  assert.equal(REVIEW_HEADER, "Change set waiting for your decision");
  assert.ok(
    html.includes(
      '<div class="meta"><span>Project <b>Vehicle Control</b></span>' +
        "<span>Client <b>Codex</b></span>" +
        "<span>Change set <b>cs-20261025T120000Z-0a1b2c</b></span>" +
        "<span>Waiting since <b>12:00</b></span></div>",
    ),
  );
});

test("reviewHtml: without a client the whole Client item is left out", () => {
  const html = reviewHtml(record({ client: null }), { nonce: "abc123", utcOffsetMinutes: 90 });
  assert.ok(!html.includes("Client"));
  assert.deepEqual(
    metaItems(record({ client: null }), 90).map((item) => item.label),
    ["Project", "Change set", "Waiting since"],
  );
  assert.ok(html.includes("<span>Waiting since <b>13:30</b></span>"));
});

test("clockTime: HH:MM at the offset, across midnight", () => {
  assert.equal(clockTime("2026-10-26T12:10:00Z", 0), "12:10");
  assert.equal(clockTime("2026-10-26T23:50:00Z", 60), "00:50");
});

test("metaItems: Waiting since is the record's created, and names no deadline", () => {
  const html = reviewHtml(record({ created: "2026-10-26T12:15:00Z" }), {
    nonce: "abc123",
    utcOffsetMinutes: 0,
  });
  assert.ok(html.includes("<span>Waiting since <b>12:15</b></span>"));
  assert.ok(!html.includes("min)"));
  assert.ok(!/deadline|rejected|Waits until/.test(html.slice(0, html.indexOf("<script"))));
});

test("reviewHtml: a project or client name carrying <, & or a newline comes out flat and escaped", () => {
  const html = reviewHtml(
    record({ project: { name: "A<b>&\nC", manifest: "/p" }, client: "Co<dex>&\n'x'" }),
    { nonce: "abc123", utcOffsetMinutes: 0 },
  );
  assert.ok(html.includes("<span>Project <b>A&lt;b&gt;&amp; C</b></span>"));
  assert.ok(html.includes("<span>Client <b>Co&lt;dex&gt;&amp; &#39;x&#39;</b></span>"));
  assert.ok(!html.includes("<dex>"));
});

test("reviewHtml: the buttons and the waiting sentence, and nothing else", () => {
  const html = reviewHtml(record(), { nonce: "abc123", utcOffsetMinutes: 0 });
  assert.ok(
    html.includes(
      '<button type="button" class="approve" data-decision="approve">✓ Approve</button>',
    ),
  );
  assert.ok(
    html.includes('<button type="button" class="reject" data-decision="reject">✕ Reject</button>'),
  );
  assert.equal(
    WAITING_LINE,
    "The assistant waits until you answer. Closing this tab does not answer; the Pending Change Sets view reopens it.",
  );
  assert.ok(
    html.includes(
      '<span id="status" class="wait" role="status">The assistant waits until you answer. Closing this tab does not answer; the Pending Change Sets view reopens it.</span>',
    ),
  );
  const body = html.slice(html.indexOf("<body>"), html.indexOf("<script"));
  const tags = [...body.matchAll(/<([a-z0-9]+)/g)].map((match) => match[1]);
  assert.deepEqual(tags, [
    "body",
    "header",
    "h1",
    "div",
    "span",
    "b",
    "span",
    "b",
    "span",
    "b",
    "span",
    "b",
    "pre",
    "span",
    "div",
    "button",
    "button",
    "span",
  ]);
});

test("textHtml: a tint on the operation word of a create, update and remove line only", () => {
  const inner = textHtml(DELTA_TEXT);
  assert.ok(
    inner.includes(
      '  1. <span class="op-create">create</span> interfaces/If_BrakeTemperature — sender-receiver',
    ),
  );
  assert.ok(inner.includes('  2. <span class="op-update">update</span> swc/BrakeController'));
  assert.ok(inner.includes('  10. <span class="op-remove">remove</span> connectors/Conn_Old'));
  assert.equal(inner.match(/<span class="op-/g)?.length, 3, "no other line is tinted");
  assert.ok(
    inner.includes("\n  created by update remove\n"),
    "a line that is no entry stays plain",
  );
  assert.ok(inner.includes("Intent: Add a brake temperature signal\n"));
});

test("textHtml: the last non-empty line, the question, is emphasised", () => {
  const inner = textHtml(DELTA_TEXT);
  assert.ok(
    inner.endsWith(
      '<span class="q">Nothing has been applied yet and the model is byte-identical. Commit exactly this delta?</span>',
    ),
  );
  assert.equal(inner.match(/<span class="q">/g)?.length, 1);
  assert.ok(
    textHtml("one\nApprove the weakening as well?\n\n").endsWith(
      '<span class="q">Approve the weakening as well?</span>\n\n',
    ),
  );
  assert.equal(textHtml(""), "");
});

test("reviewHtml: the <pre>'s textContent is the text, byte for byte", () => {
  const hostile = "<script>alert(1)</script>\n  1. create a & b </pre>\n\u00a0 2. remove 'x'\n";
  for (const text of [DELTA_TEXT, PYTHON_TEXT, hostile, "", "\n\n", "  3. update"]) {
    const html = reviewHtml(withText(text), { nonce: "abc123", utcOffsetMinutes: 0 });
    assert.equal(textContentOf(preInner(html)), text, JSON.stringify(text));
  }
});

// --- The warning above the delta ---------------------------------------------

/** The warning vector, read through the one parser. */
function warningRecord(): PendingRecord {
  const parsed = parsePending(WARNING_RECORD);
  assert.ok(parsed.ok);
  return parsed.record;
}

test("parsePending: the record with a warning is read, and its lines are the block", () => {
  const warning = warningRecord();
  const [start, end] = warning.warning_lines as [number, number];
  const lines = warning.text.split("\n");
  assert.equal(lines[start], "WARNING: more than the delta will change in 1 file(s).");
  assert.ok(lines[end - 1].startsWith("  - swc/BrakeController: "));
  assert.equal(textDigest(warning.text), warning.digest);
});

test("parsePending: warning_lines is optional and still no other key is admitted", () => {
  assert.equal(record().warning_lines, undefined);
  const extended = JSON.parse(WARNING_RECORD);
  extended.extra = 1;
  assert.deepEqual(parsePending(JSON.stringify(extended)), { ok: false, reason: "an unknown key" });
});

test("parsePending: a malformed warning_lines breaks no record", () => {
  for (const bad of [null, "4", 4, {}, [], [1], [1, 2, 3], ["a", "b"], [6, 2], [-1, 2], [0, 1e9]]) {
    const parsed = parsePending(
      JSON.stringify({ ...JSON.parse(WARNING_RECORD), warning_lines: bad }),
    );
    assert.ok(parsed.ok, JSON.stringify(bad));
    assert.ok(!textHtml(parsed.record.text, parsed.record.warning_lines).includes('class="warn"'));
  }
});

test("warningSpan: a range of the text, or nothing", () => {
  assert.deepEqual(warningSpan([1, 3], 3), [1, 3]);
  assert.deepEqual(warningSpan([0, 1], 1), [0, 1]);
  for (const bad of [
    undefined,
    null,
    [3, 3],
    [3, 1],
    [0, 4],
    [-1, 1],
    [0.5, 2],
    ["1", "2"],
    [1, 2, 3],
    [Number.NaN, 2],
  ]) {
    assert.equal(warningSpan(bad, 3), undefined, JSON.stringify(bad));
  }
});

test("textHtml: the warning's lines are one span, from the field", () => {
  const warning = warningRecord();
  const [start, end] = warning.warning_lines as [number, number];
  const inner = textHtml(warning.text, warning.warning_lines);
  const lines = warning.text.split("\n");
  assert.equal(inner.match(/<span class="warn">/g)?.length, 1);
  assert.ok(inner.includes(`\n<span class="warn">${escapeHtml(lines[start])}\n`));
  assert.ok(inner.includes(`${escapeHtml(lines[end - 1])}</span>\n`));
});

test("reviewHtml: the warning leaves the <pre>'s textContent the text, byte for byte", () => {
  const warning = warningRecord();
  const html = reviewHtml(warning, { nonce: "abc123", utcOffsetMinutes: 0 });
  assert.ok(html.includes('<span class="warn">'));
  assert.equal(textContentOf(preInner(html)), warning.text);
  assert.equal(textDigest(textContentOf(preInner(html))), warning.digest);
});

test("reviewHtml: the warning mark is a pseudo-element, outside textContent", () => {
  const warning = warningRecord();
  const html = reviewHtml(warning, { nonce: "abc123", utcOffsetMinutes: 0 });
  assert.ok(html.includes('.warn::before { content: "⚠ "'));
  assert.ok(!preInner(html).includes("⚠"));
});

test("textHtml: text spelling WARNING: with no field gets no highlight", () => {
  const forged = [
    "Intent: WARNING: more than the delta will change in 9 file(s).",
    "WARNING: more than the delta will change in 9 file(s).",
    "",
    "Commit exactly this delta and rewrite 9 file(s) whole?",
  ].join("\n");
  const html = reviewHtml(withText(forged), { nonce: "abc123", utcOffsetMinutes: 0 });
  assert.ok(!preInner(html).includes('class="warn"'));
  assert.equal(textContentOf(preInner(html)), forged);
});

test("answerPlan: an approval on a warning binds the same digest", () => {
  const warning = warningRecord();
  assert.deepEqual(
    answerPlan(warning, warningRecord(), { decision: "approve", digest: warning.digest }),
    {
      ok: true,
      record: warning,
      decision: "approve",
      digest: warning.digest,
    },
  );
});

test("reviewHtml: the page's style names no hard-coded colour", () => {
  const html = reviewHtml(record(), { nonce: "abc123", utcOffsetMinutes: 0 });
  const style = html.slice(html.indexOf("<style"), html.indexOf("</style>"));
  assert.ok(!/#[0-9a-f]{3,8}\b/i.test(style), "a hex colour");
  assert.ok(!/\b(rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(/i.test(style), "a colour function");
  assert.ok(
    !/:\s*(white|black|red|green|blue|yellow|gray|grey|orange|purple)\b/i.test(style),
    "a named colour",
  );
  for (const variable of [
    "--vscode-editor-background",
    "--vscode-foreground",
    "--vscode-descriptionForeground",
    "--vscode-textCodeBlock-background",
    "--vscode-button-background",
    "--vscode-button-secondaryBackground",
    "--vscode-charts-green",
    "--vscode-charts-blue",
    "--vscode-charts-red",
    "--vscode-inputValidation-warningBackground",
    "--vscode-inputValidation-warningBorder",
    "--vscode-editorWarning-foreground",
    "--vscode-editor-font-family",
  ]) {
    assert.ok(style.includes(`var(${variable})`), variable);
  }
});

test("reviewHtml: the <pre> wraps a long line, by its CSS rule", () => {
  const html = reviewHtml(record(), { nonce: "abc123", utcOffsetMinutes: 0 });
  const style = html.slice(html.indexOf("<style"), html.indexOf("</style>"));
  const rule = style.match(/^pre \{([^}]*)\}/m)?.[1];
  assert.ok(rule !== undefined, "the page has a rule for the <pre>");
  assert.ok(/\bwhite-space:\s*pre-wrap\s*;/.test(rule), rule);
  assert.ok(/\boverflow-wrap:\s*break-word\s*;/.test(rule), rule);
});

function scriptCount(html: string): number {
  return html.match(/<script/g)?.length ?? 0;
}

test("reviewHtml: hostile text comes out escaped inside the one <pre>", () => {
  const text = "<script>alert(1)</script>\na & b\n</pre><b>x</b>\n```js\nevil()\n```\n";
  const html = reviewHtml(withText(text), { nonce: "abc123", utcOffsetMinutes: 0 });
  assert.equal(scriptCount(html), 1);
  assert.equal(html.match(/<script nonce="abc123">/g)?.length, 1);
  assert.equal(html.match(/<pre/g)?.length, 1);
  assert.equal(html.match(/<\/pre>/g)?.length, 1);
  assert.ok(html.includes("&lt;script&gt;alert(1)&lt;/script&gt;"));
  assert.ok(html.includes("a &amp; b"));
  assert.ok(html.includes("&lt;/pre&gt;&lt;b&gt;x&lt;/b&gt;"));
  assert.ok(html.includes("\n```js\n"));
  assert.ok(!html.includes("<b>x</b>"));
});

test("reviewHtml: a text that starts with a newline keeps it (the newline after <pre>)", () => {
  const html = reviewHtml(record(), { nonce: "abc123", utcOffsetMinutes: 0 });
  // PYTHON_TEXT starts with "\n": the parser drops the first newline, the text's own stays.
  assert.ok(html.includes(`<pre id="text">\n\nChange set`));
});

test("reviewHtml: the CSP is default-src 'none' with the nonce; no autofocus; both buttons", () => {
  const html = reviewHtml(record(), { nonce: "N0nce+/=", utcOffsetMinutes: 0 });
  const csp = "default-src 'none'; style-src 'nonce-N0nce+/='; script-src 'nonce-N0nce+/='";
  assert.equal(reviewCsp("N0nce+/="), csp);
  assert.ok(html.includes(`<meta http-equiv="Content-Security-Policy" content="${csp}">`));
  assert.ok(html.includes('<style nonce="N0nce+/=">'));
  assert.ok(!/autofocus/i.test(html));
  assert.equal(html.match(/<button /g)?.length, 2);
  assert.ok(!/\son[a-z]+=/i.test(html), "no inline event handler");
  assert.ok(!/\ssrc=|\shref=/i.test(html), "no resource is loaded");
});

test("reviewHtml: a nonce outside base64 is refused; newNonce is base64 and fresh", () => {
  assert.throws(() => reviewHtml(record(), { nonce: 'a"b', utcOffsetMinutes: 0 }));
  const one = newNonce();
  assert.match(one, /^[A-Za-z0-9+/]+={0,2}$/);
  assert.notEqual(one, newNonce());
});

// The tab's script, run against a stand-in for the page: what it posts is the
// digest of the <pre>'s textContent, computed by crypto.subtle, never read
// from the page; a "disable" message disables both buttons.
async function runScript(textContent: string) {
  const posted: unknown[] = [];
  const listeners: Record<string, (event: { data: unknown }) => void> = {};
  const button = (decision: string) => {
    const handlers: (() => void)[] = [];
    return {
      dataset: { decision },
      disabled: false,
      addEventListener: (_: string, handler: () => void) => handlers.push(handler),
      click: () => {
        for (const handler of handlers) handler();
      },
    };
  };
  const approve = button("approve");
  const reject = button("reject");
  const status = { textContent: "" };
  const document = {
    getElementById: (id: string) => (id === "text" ? { textContent } : status),
    querySelectorAll: () => [approve, reject],
  };
  const window = {
    addEventListener: (type: string, handler: (event: { data: unknown }) => void) => {
      listeners[type] = handler;
    },
  };
  const acquireVsCodeApi = () => ({ postMessage: (message: unknown) => posted.push(message) });
  new Function("acquireVsCodeApi", "document", "window", "crypto", REVIEW_SCRIPT)(
    acquireVsCodeApi,
    document,
    window,
    globalThis.crypto,
  );
  return { posted, approve, reject, status, listeners };
}

async function settle(posted: unknown[]): Promise<void> {
  for (let i = 0; i < 100 && posted.length === 0; i++) {
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
}

test("the tab's script posts the decision and the sha256 of the shown text", async () => {
  const page = await runScript(PYTHON_TEXT);
  page.approve.click();
  await settle(page.posted);
  assert.deepEqual(page.posted, [{ decision: "approve", digest: PYTHON_DIGEST }]);
  assert.ok(page.approve.disabled && page.reject.disabled);

  const other = await runScript(PYTHON_TEXT);
  other.reject.click();
  await settle(other.posted);
  assert.deepEqual(other.posted, [{ decision: "reject", digest: PYTHON_DIGEST }]);
});

test("the tab's script: a disable message disables both buttons and shows its line", async () => {
  const page = await runScript("x");
  page.listeners.message({ data: { type: "disable", line: "No longer waiting." } });
  assert.ok(page.approve.disabled && page.reject.disabled);
  assert.equal(page.status.textContent, "No longer waiting.");
});

test("the tab's script: a note message makes both buttons live again and shows its line", async () => {
  const page = await runScript("x");
  page.approve.click();
  await settle(page.posted);
  assert.ok(page.approve.disabled && page.reject.disabled, "a click disables the buttons");
  page.listeners.message({ data: { type: "note", line: EARLIER_ANSWER_IN_PLACE } });
  assert.ok(!page.approve.disabled && !page.reject.disabled);
  assert.equal(page.status.textContent, EARLIER_ANSWER_IN_PLACE);
});

// --- textDigest and checkApproval -------------------------------------------

test("textDigest: the Python-computed vector over a non-ASCII text", () => {
  assert.equal(textDigest(PYTHON_TEXT), PYTHON_DIGEST);
});

test("checkApproval: all three digests equal is the digest to write", () => {
  assert.deepEqual(checkApproval(record(), PYTHON_TEXT, PYTHON_DIGEST), {
    ok: true,
    digest: PYTHON_DIGEST,
  });
});

test("checkApproval: the posted digest differs → refused", () => {
  const check = checkApproval(record(), PYTHON_TEXT, textDigest("other"));
  assert.deepEqual(check, {
    ok: false,
    reason: "The text this tab shows does not match the waiting question.",
  });
  assert.equal(checkApproval(record(), PYTHON_TEXT, undefined).ok, false);
});

test("checkApproval: the rendered text differs → refused", () => {
  assert.deepEqual(checkApproval(record(), "another text", PYTHON_DIGEST), {
    ok: false,
    reason: "The question changed since this tab was opened.",
  });
});

test("checkApproval: the record's digest differs from its text → refused", () => {
  const forged = record({ digest: textDigest("something else") });
  assert.deepEqual(checkApproval(forged, PYTHON_TEXT, PYTHON_DIGEST), {
    ok: false,
    reason: "The waiting question's digest does not match its text.",
  });
});

// --- reviewNodes ------------------------------------------------------------

test("reviewNodes: label, description and tooltip", () => {
  const [node] = reviewNodes([record()], 0);
  assert.deepEqual(node, {
    key: "cs-20261025T120000Z-0a1b2c.delta", // gitleaks:allow -- a review node key (change set id), not a secret
    changeSetId: "cs-20261025T120000Z-0a1b2c",
    question: "delta",
    label: "Add the wheel speed port",
    description: "delta question, since 12:00",
    tooltip: "cs-20261025T120000Z-0a1b2c\nVehicle Control",
  });
});

test("reviewNodes: no intent is '(no intent given)'; every record given is a row, at the offset", () => {
  // The records come from windowReviews, already live: the view filters nothing again.
  const nodes = reviewNodes(
    [
      record({ intent: null, question: "safety" }),
      record({
        intent: "",
        change_set_id: "cs-20261025T120001Z-0a1b2c",
        created: "2026-10-26T11:00:00Z",
      }),
    ],
    60,
  );
  assert.deepEqual(
    nodes.map((node) => [node.key, node.label, node.description]),
    [
      ["cs-20261025T120000Z-0a1b2c.safety", NO_INTENT, "safety question, since 13:00"],
      ["cs-20261025T120001Z-0a1b2c.delta", NO_INTENT, "delta question, since 12:00"],
    ],
  );
});

test("reviewNodes: a project name with a newline is flat in the tooltip", () => {
  const [node] = reviewNodes([record({ project: { name: "A\nB", manifest: "/p" } })], 0);
  assert.equal(node.tooltip, "cs-20261025T120000Z-0a1b2c\nA B");
});

// --- decisionRecord, the file names and the home ----------------------------

test("decisionRecord: exactly the six keys, in order, with a trailing newline", () => {
  const json = decisionRecord(record(), "approve", PYTHON_DIGEST);
  assert.equal(
    json,
    `{
  "format": 2,
  "change_set_id": "cs-20261025T120000Z-0a1b2c",
  "question": "delta",
  "wait": "${PYTHON_WAIT}",
  "digest": "${PYTHON_DIGEST}",
  "decision": "approve"
}
`,
  );
  assert.deepEqual(Object.keys(JSON.parse(json)), [
    "format",
    "change_set_id",
    "question",
    "wait",
    "digest",
    "decision",
  ]);
  assert.equal(JSON.parse(decisionRecord(record(), "reject", PYTHON_DIGEST)).decision, "reject");
});

test("decisionRecord: a digest other than the record's is refused", () => {
  assert.throws(() => decisionRecord(record(), "approve", textDigest("other")));
});

test("the file names are panel_review's", () => {
  assert.equal(reviewKey(record()), "cs-20261025T120000Z-0a1b2c.delta");
  assert.equal(pendingFileName(record()), "cs-20261025T120000Z-0a1b2c.delta.pending.json");
  assert.equal(decisionFileName(record()), "cs-20261025T120000Z-0a1b2c.delta.decision.json");
  assert.equal(
    decisionFileName(record({ question: "safety" })),
    "cs-20261025T120000Z-0a1b2c.safety.decision.json",
  );
});

test("reviewsDir: under the same home literals as panel_review's tests", () => {
  assert.equal(
    reviewsDir(toolchainHome({ LOCALAPPDATA: "/somewhere/else" }, "/Users/dev", "posix"), "posix"),
    "/Users/dev/.swcraft/reviews",
  );
  assert.equal(
    reviewsDir(
      toolchainHome(
        { LOCALAPPDATA: "C:\\Users\\dev\\AppData\\Local" },
        "C:\\Users\\dev",
        "windows",
      ),
      "windows",
    ),
    "C:\\Users\\dev\\AppData\\Local\\SWCraft\\reviews",
  );
  assert.equal(
    reviewsDir(
      toolchainHome({ USERPROFILE: "C:\\Users\\dev" }, "C:\\Users\\other", "windows"),
      "windows",
    ),
    "C:\\Users\\dev\\AppData\\Local\\SWCraft\\reviews",
  );
  assert.equal(
    reviewsDir(toolchainHome({}, "C:\\Users\\dev", "windows"), "windows"),
    "C:\\Users\\dev\\AppData\\Local\\SWCraft\\reviews",
  );
});

// --- writeDecision on a real directory --------------------------------------
// These run on both CI runners, which is the Windows evidence for linkSync's
// refusal of an existing target.

function tempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-vscode-reviewCore-test-"));
}

test("writeDecision: the final file is in place, byte-exact, and no *.tmp is left", () => {
  const dir = tempDir();
  try {
    const outcome = writeDecision(fs, dir, record(), "approve", PYTHON_DIGEST);
    const final = path.join(dir, "cs-20261025T120000Z-0a1b2c.delta.decision.json");
    assert.deepEqual(outcome, { ok: true, file: final });
    assert.equal(
      fs.readFileSync(final, { encoding: "utf8" }),
      decisionRecord(record(), "approve", PYTHON_DIGEST),
    );
    assert.deepEqual(fs.readdirSync(dir), ["cs-20261025T120000Z-0a1b2c.delta.decision.json"]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeDecision: the decision file is mode 0o600", {
  skip: process.platform === "win32" ? "Windows ignores POSIX modes" : false,
}, () => {
  const dir = tempDir();
  try {
    writeDecision(fs, dir, record(), "reject", PYTHON_DIGEST);
    const final = path.join(dir, decisionFileName(record()));
    assert.equal(fs.statSync(final).mode & 0o777, 0o600);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeDecision: a second write to the same name is refused and the first answer stays", () => {
  const dir = tempDir();
  try {
    assert.ok(writeDecision(fs, dir, record(), "reject", PYTHON_DIGEST).ok);
    assert.deepEqual(writeDecision(fs, dir, record(), "approve", PYTHON_DIGEST), {
      ok: false,
      reason: ALREADY_ANSWERED,
    });
    const final = path.join(dir, decisionFileName(record()));
    assert.equal(JSON.parse(fs.readFileSync(final, { encoding: "utf8" })).decision, "reject");
    assert.deepEqual(fs.readdirSync(dir), [decisionFileName(record())]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeDecision: an answer to an earlier copy of the question keeps the tab live", () => {
  // The server puts a swept record back under a fresh wait token and clears the old answer only
  // after that link. A click in between meets the old answer: it answers nothing of this record,
  // so the refusal leaves the buttons live, the old file is not touched, and the next click, once
  // the server cleared it, is written.
  const dir = tempDir();
  try {
    const earlier = { ...record(), wait: "0123456789abcdef0123456789abcdef" };
    assert.notEqual(earlier.wait, record().wait);
    assert.ok(writeDecision(fs, dir, earlier, "reject", PYTHON_DIGEST).ok);
    const final = path.join(dir, decisionFileName(record()));
    const old = fs.readFileSync(final, { encoding: "utf8" });
    assert.deepEqual(writeDecision(fs, dir, record(), "approve", PYTHON_DIGEST), {
      ok: false,
      reason: EARLIER_ANSWER_IN_PLACE,
      live: true,
    });
    assert.equal(fs.readFileSync(final, { encoding: "utf8" }), old);
    assert.deepEqual(fs.readdirSync(dir), [decisionFileName(record())]);
    fs.unlinkSync(final); // the server's clearing after its link
    assert.ok(writeDecision(fs, dir, record(), "approve", PYTHON_DIGEST).ok);
    assert.equal(JSON.parse(fs.readFileSync(final, { encoding: "utf8" })).wait, record().wait);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeDecision: a file at the name that is no decision keeps the tab live", () => {
  const dir = tempDir();
  try {
    fs.writeFileSync(path.join(dir, decisionFileName(record())), "{ not json", {
      encoding: "utf8",
    });
    assert.deepEqual(writeDecision(fs, dir, record(), "reject", PYTHON_DIGEST), {
      ok: false,
      reason: EARLIER_ANSWER_IN_PLACE,
      live: true,
    });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("answersRecord: only the record's own change set, question and wait token answer it", () => {
  const dir = tempDir();
  try {
    const file = path.join(dir, decisionFileName(record()));
    const write = (document: unknown) =>
      fs.writeFileSync(file, JSON.stringify(document), { encoding: "utf8" });
    const own = JSON.parse(decisionRecord(record(), "reject", PYTHON_DIGEST));
    write(own);
    assert.equal(answersRecord(fs, file, record()), true);
    write({ ...own, wait: "f".repeat(32) });
    assert.equal(answersRecord(fs, file, record()), false);
    write({ ...own, question: "safety" });
    assert.equal(answersRecord(fs, file, record()), false);
    write([own]);
    assert.equal(answersRecord(fs, file, record()), false);
    fs.unlinkSync(file);
    assert.equal(answersRecord(fs, file, record()), false, "a file gone since the refusal");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

/** A `DecisionFs` over the real one that records each call's file name. */
function recordingFs(): { fs: DecisionFs; written: string[]; flags: unknown[]; removed: string[] } {
  const written: string[] = [];
  const flags: unknown[] = [];
  const removed: string[] = [];
  const wrapped: DecisionFs = {
    writeFileSync: ((file: fs.PathOrFileDescriptor, data: string, options: fs.WriteFileOptions) => {
      written.push(path.basename(String(file)));
      flags.push(typeof options === "object" && options !== null ? options.flag : undefined);
      fs.writeFileSync(file, data, options);
    }) as typeof fs.writeFileSync,
    linkSync: fs.linkSync,
    readFileSync: fs.readFileSync,
    unlinkSync: ((file: fs.PathLike) => {
      removed.push(path.basename(String(file)));
      fs.unlinkSync(file);
    }) as typeof fs.unlinkSync,
  };
  return { fs: wrapped, written, flags, removed };
}

test("writeDecision: every write draws a temporary name of its own, created exclusively", () => {
  const dir = tempDir();
  try {
    const recorder = recordingFs();
    assert.ok(writeDecision(recorder.fs, dir, record(), "reject", PYTHON_DIGEST).ok);
    fs.unlinkSync(path.join(dir, decisionFileName(record())));
    assert.ok(writeDecision(recorder.fs, dir, record(), "approve", PYTHON_DIGEST).ok);
    assert.equal(recorder.written.length, 2);
    assert.notEqual(recorder.written[0], recorder.written[1], "unique per write, not per process");
    const shape = new RegExp(
      `^\\.${decisionFileName(record()).replace(/\./g, "\\.")}\\.${process.pid}\\.[0-9a-f]{16}\\.tmp$`,
    );
    for (const name of recorder.written) {
      assert.match(name, shape);
      assert.ok(!isPendingFileName(name));
    }
    assert.deepEqual(recorder.flags, ["wx", "wx"]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeDecision: a temporary name already taken is neither truncated nor removed", () => {
  const dir = tempDir();
  try {
    const taken: string[] = [];
    const collides: DecisionFs = {
      // The exclusive create meets another write's file at the drawn name.
      writeFileSync: ((file: fs.PathOrFileDescriptor) => {
        taken.push(String(file));
        fs.writeFileSync(String(file), "another write's record\n", { encoding: "utf8" });
        throw Object.assign(new Error("exists"), { code: "EEXIST" });
      }) as typeof fs.writeFileSync,
      linkSync: fs.linkSync,
      readFileSync: fs.readFileSync,
      unlinkSync: fs.unlinkSync,
    };
    assert.deepEqual(writeDecision(collides, dir, record(), "approve", PYTHON_DIGEST), {
      ok: false,
      reason: "The answer could not be written (EEXIST).",
    });
    assert.equal(fs.readFileSync(taken[0], { encoding: "utf8" }), "another write's record\n");
    assert.ok(!fs.existsSync(path.join(dir, decisionFileName(record()))));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeDecision: another failure is a refusal naming the code and no path", () => {
  const dir = path.join(tempDir(), "missing");
  try {
    const outcome = writeDecision(fs, dir, record(), "approve", PYTHON_DIGEST);
    assert.deepEqual(outcome, { ok: false, reason: "The answer could not be written (ENOENT)." });
  } finally {
    fs.rmSync(path.dirname(dir), { recursive: true, force: true });
  }
});

// --- What the host decides --------------------------------------------------

/** *value*'s file, its heartbeat (mtime) at *mtimeMs*: by default a live one at `NOW_MS`. */
function entryOf(
  value: PendingRecord,
  name = pendingFileName(value),
  mtimeMs = NOW_MS - 1_000,
): ReviewEntry {
  return { name, text: `${JSON.stringify(value, null, 2)}\n`, mtimeMs };
}

test("windowReviews: the live records of this project, under their own names, oldest first", () => {
  const manifest = "/Users/dev/p/swcraft.yaml";
  const now = NOW_MS;
  const older = record({
    change_set_id: "cs-20261026T115900Z-000001",
    created: "2026-10-26T11:59:00Z",
  });
  const delta = record();
  const safety = record({ question: "safety" });
  const elsewhere = record({
    change_set_id: "cs-20261026T120000Z-000002",
    project: { name: "Other", manifest: "/Users/dev/q/swcraft.yaml" },
  });
  const stale = record({ change_set_id: "cs-20261026T120000Z-000003" });
  const misfiled = record({ change_set_id: "cs-20261026T120000Z-000004" });
  const shown = windowReviews(
    [
      entryOf(safety),
      entryOf(delta),
      entryOf(elsewhere),
      entryOf(stale, pendingFileName(stale), now - STALE_MS),
      entryOf(misfiled, pendingFileName(record({ change_set_id: "cs-20261026T120000Z-000005" }))),
      entryOf(older),
      { ...entryOf(older), name: `.${pendingFileName(older)}.4711.tmp` },
      { ...entryOf(delta), name: decisionFileName(delta), text: "{}" },
      {
        ...entryOf(delta),
        name: "cs-20261026T120000Z-000006.delta.pending.json",
        text: "not json",
      },
    ],
    [manifest],
    "posix",
    now,
  );
  assert.deepEqual(
    shown.map((value) => reviewKey(value)),
    [reviewKey(older), reviewKey(delta), reviewKey(safety)],
  );
  // The Windows spelling of the same manifest is the same project.
  const windows = record({ project: { name: "P", manifest: "C:/Users/X/p/swcraft.yaml" } });
  assert.equal(
    windowReviews([entryOf(windows)], ["c:\\users\\x\\p\\swcraft.yaml"], "windows", now).length,
    1,
  );
  assert.equal(windowReviews([], [manifest], "posix", now).length, 0);
  assert.equal(
    windowReviews([entryOf(delta)], [], "posix", now).length,
    0,
    "no project, no record",
  );
});

test("windowReviews: a multi-root window shows the questions of every folder's project", () => {
  const now = NOW_MS;
  const first = record();
  const second = record({
    change_set_id: "cs-20261026T120000Z-000002",
    project: { name: "Other", manifest: "/Users/dev/q/swcraft.yaml" },
  });
  const third = record({
    change_set_id: "cs-20261026T120000Z-000003",
    project: { name: "Third", manifest: "/Users/dev/r/swcraft.yaml" },
  });
  const shown = windowReviews(
    [entryOf(first), entryOf(second), entryOf(third)],
    ["/Users/dev/p/swcraft.yaml", "/Users/dev/q/swcraft.yaml"],
    "posix",
    now,
  );
  assert.deepEqual(shown.map(reviewKey), [reviewKey(first), reviewKey(second)]);
});

test("windowReviews: the record's manifest is canonicalised before it is compared", () => {
  // macOS: the server's `Path.resolve()` keeps the letter case it was given,
  // and the window's real path is the case on disk.
  const now = NOW_MS;
  const spelled = record({ project: { name: "P", manifest: "/users/DEV/p/swcraft.yaml" } });
  const onDisk = "/Users/dev/p/swcraft.yaml";
  assert.equal(windowReviews([entryOf(spelled)], [onDisk], "posix", now).length, 0);
  const canonical = (manifest: string): string =>
    manifest.toLowerCase() === onDisk.toLowerCase() ? onDisk : manifest;
  assert.equal(windowReviews([entryOf(spelled)], [onDisk], "posix", now, canonical).length, 1);
});

test("windowReviews: an old heartbeat hides a record, whatever its created; a fresh one shows it", () => {
  const manifest = "/Users/dev/p/swcraft.yaml";
  const delta = record();
  const at = (mtimeMs: number, now: number): number =>
    windowReviews([entryOf(delta, pendingFileName(delta), mtimeMs)], [manifest], "posix", now)
      .length;
  assert.equal(at(NOW_MS - (STALE_MS - 1), NOW_MS), 1);
  assert.equal(at(NOW_MS - STALE_MS, NOW_MS), 0, "four missed heartbeats: no live wait");
  assert.equal(at(CREATED_MS, NOW_MS), 0, "a record never beaten since it was put is stale");
  const dayLater = CREATED_MS + 86_400_000;
  assert.equal(at(dayLater - 15_000, dayLater), 1, "a day into the wait, a beating record shows");
});

test("isPendingFileName: pending records only, never a temporary file or a decision", () => {
  assert.ok(isPendingFileName("cs-20261025T120000Z-0a1b2c.delta.pending.json"));
  assert.ok(!isPendingFileName(".cs-20261025T120000Z-0a1b2c.delta.pending.json.4711.tmp"));
  assert.ok(!isPendingFileName(".cs-20261025T120000Z-0a1b2c.delta.pending.json"));
  assert.ok(!isPendingFileName("cs-20261025T120000Z-0a1b2c.delta.decision.json"));
});

test("compareRecords and intentLabel: the one order and the one label", () => {
  const a = record({ created: "2026-10-26T11:00:00Z" });
  const b = record();
  assert.ok(compareRecords(a, b) < 0);
  assert.ok(compareRecords(b, record({ question: "safety" })) < 0);
  assert.equal(compareRecords(b, b), 0);
  assert.equal(intentLabel(record({ intent: null })), NO_INTENT);
  assert.equal(intentLabel(record({ intent: "a\nb" })), "a b");
});

// --- reviewTransitions ------------------------------------------------------

/** A map of each key to an identity of its own, the shape `reviewTransitions` reads. */
function ids(...keys: string[]): Map<string, string> {
  return new Map(keys.map((key) => [key, `id:${key}`]));
}

/** Open tabs, each rendering the record `ids` gives its key, disabled as given. */
function tabsOf(
  entries: Record<string, string | undefined>,
): Map<string, { identity: string; disabled?: string }> {
  return new Map(
    Object.entries(entries).map(([key, disabled]) => [key, { identity: `id:${key}`, disabled }]),
  );
}

test("reviewIdentity: the fields that make one wait, and no field beside them", () => {
  const shown = record();
  assert.ok(sameReview(shown, record()));
  assert.ok(
    sameReview(shown, record({ intent: "another intent", client: null })),
    "a field outside the identity does not make another wait",
  );
  const others: Partial<PendingRecord>[] = [
    { change_set_id: "cs-20261025T120000Z-ffffff" },
    { question: "safety" },
    { wait: OTHER_WAIT },
    { project: { name: shown.project.name, manifest: "/Users/dev/q/swcraft.yaml" } },
    { baseline: "sha256:0000" },
    { digest: textDigest("x") },
    { created: "2026-10-25T12:00:01Z" },
  ];
  for (const overrides of others) {
    assert.equal(sameReview(shown, record(overrides)), false, JSON.stringify(overrides));
    assert.notEqual(reviewIdentity(shown), reviewIdentity(record(overrides)));
  }
});

test("reviewTransitions: a live question new to the window opens, once", () => {
  const first = reviewTransitions(ids("k1", "k2"), new Map(), new Map(), new Set(["k1", "k2"]));
  assert.deepEqual(first.open, ["k1", "k2"]);
  const again = reviewTransitions(
    ids("k1", "k2"),
    ids("k1", "k2"),
    tabsOf({ k1: undefined }),
    new Set(["k1", "k2"]),
  );
  assert.deepEqual(again.open, [], "a re-read never reopens, a closed tab (k2) included");
});

test("reviewTransitions: a vanished question disables its open tab, once", () => {
  const vanished = reviewTransitions(new Map(), ids("k1"), tabsOf({ k1: undefined }), new Set());
  assert.deepEqual(vanished.disable, ["k1"]);
  const already = reviewTransitions(
    new Map(),
    ids("k1"),
    tabsOf({ k1: NO_LONGER_WAITING }),
    new Set(),
  );
  assert.deepEqual(already.disable, [], "a disabled tab is not disabled again");
});

test("reviewTransitions: a question live again re-enables a tab only it disabled", () => {
  const tabs = tabsOf({ back: NO_LONGER_WAITING, answered: ALREADY_ANSWERED, open: undefined });
  const transitions = reviewTransitions(
    ids("back", "answered", "open"),
    ids("back", "answered", "open"),
    tabs,
    new Set(tabs.keys()),
  );
  assert.deepEqual(transitions.enable, ["back"]);
  assert.deepEqual(transitions.disable, []);
  assert.deepEqual(transitions.dispose, []);
});

test("reviewTransitions: another record at a tab's key disposes the stale tab and opens the new one", () => {
  // A wait ended and its tab was disabled; another project's server drew the
  // same change-set id in the same second and filed its record under that key.
  const live = new Map([
    ["reused", "id:another-wait"],
    ["reused-enabled", "id:another-wait-2"],
  ]);
  const tabs = tabsOf({ reused: NO_LONGER_WAITING, "reused-enabled": undefined });
  const transitions = reviewTransitions(
    live,
    ids("reused", "reused-enabled"),
    tabs,
    new Set(live.keys()),
  );
  assert.deepEqual(transitions.enable, [], "the stale tab is never enabled for the new record");
  assert.deepEqual(transitions.disable, []);
  assert.deepEqual(transitions.dispose, ["reused", "reused-enabled"]);
  assert.deepEqual(transitions.open, ["reused", "reused-enabled"], "the new record counts as new");
});

test("reviewTransitions: a closed tab's key holding another record opens it, though the key was opened", () => {
  const transitions = reviewTransitions(
    new Map([["k", "id:new"]]),
    new Map([["k", "id:old"]]),
    new Map(),
    new Set(["k"]),
  );
  assert.deepEqual(transitions.open, ["k"]);
  assert.deepEqual(transitions.dispose, []);
});

test("reviewTransitions: an opened question neither live nor in a tab is forgotten", () => {
  const transitions = reviewTransitions(
    ids("live"),
    ids("live", "closed-and-gone", "open-and-gone", "closed-but-listed"),
    tabsOf({ "open-and-gone": undefined }),
    new Set(["live", "closed-but-listed"]),
  );
  assert.deepEqual(transitions.forget, ["closed-and-gone"]);
  assert.deepEqual(transitions.disable, ["open-and-gone"]);
});

test("reviewTransitions: a listing that failed forgets nothing, so a closed tab is not reopened", () => {
  // The user closed the tab of a question still waiting; a re-read fails for a
  // moment (nothing live, nothing listed), then a good one finds it again.
  const failed = reviewTransitions(new Map(), ids("closed"), new Map(), undefined);
  assert.deepEqual(failed.forget, []);
  const good = reviewTransitions(ids("closed"), ids("closed"), new Map(), new Set(["closed"]));
  assert.deepEqual(good.open, [], "the question is still known as opened");
});

test("reviewTransitions: a stale-then-live re-read of the same identity re-enables its tab", () => {
  // A wake from sleep: every heartbeat is old until its server's first tick.
  const manifest = "/Users/dev/p/swcraft.yaml";
  const delta = record();
  const key = reviewKey(delta);
  const identity = reviewIdentity(delta);
  const liveAt = (mtimeMs: number): Map<string, string> =>
    new Map(
      windowReviews(
        [entryOf(delta, pendingFileName(delta), mtimeMs)],
        [manifest],
        "posix",
        NOW_MS,
      ).map((value) => [reviewKey(value), reviewIdentity(value)]),
    );
  const opened = new Map([[key, identity]]);
  const stale = reviewTransitions(
    liveAt(NOW_MS - 10 * STALE_MS),
    opened,
    new Map([[key, { identity }]]),
    new Set([key]),
  );
  assert.deepEqual(stale.disable, [key]);
  const back = reviewTransitions(
    liveAt(NOW_MS - 100),
    opened,
    new Map([[key, { identity, disabled: NO_LONGER_WAITING }]]),
    new Set([key]),
  );
  assert.deepEqual(back.enable, [key]);
  assert.deepEqual(back.open, [], "the same identity is not opened again");
  assert.deepEqual(back.dispose, []);
});

test("reviewTransitions: a record restored under a fresh wait token opens afresh, the old tab answered, closed or open", () => {
  const swept = record();
  const restored = record({ wait: OTHER_WAIT });
  const key = reviewKey(swept);
  assert.equal(reviewKey(restored), key);
  const live = new Map([[key, reviewIdentity(restored)]]);
  const opened = new Map([[key, reviewIdentity(swept)]]);
  // Answered, then closed: no tab, the key was opened for the swept record.
  const closed = reviewTransitions(live, opened, new Map(), new Set([key]));
  assert.deepEqual(closed.open, [key]);
  assert.deepEqual(closed.dispose, []);
  // Answered and still open, or still open and unanswered: disposed, then opened.
  for (const disabled of [ALREADY_ANSWERED, undefined, NO_LONGER_WAITING]) {
    const tabs = new Map([[key, { identity: reviewIdentity(swept), disabled }]]);
    const transitions = reviewTransitions(live, opened, tabs, new Set([key]));
    assert.deepEqual(transitions.dispose, [key], String(disabled));
    assert.deepEqual(transitions.open, [key], String(disabled));
    assert.deepEqual(transitions.enable, [], String(disabled));
  }
  // A click on the old tab re-reads the restored record and is refused.
  assert.deepEqual(answerPlan(swept, restored, { decision: "approve", digest: PYTHON_DIGEST }), {
    ok: false,
    reason: NO_LONGER_WAITING,
  });
});

test("listedReviewKeys: the keys of the pending names only, a temporary and a decision left out", () => {
  assert.deepEqual(
    [
      ...listedReviewKeys([
        "cs-20261025T120000Z-abcdef.delta.pending.json",
        "cs-20261025T120000Z-abcdef.delta.decision.json",
        ".cs-20261025T120000Z-abcdef.delta.pending.json.42.tmp",
        "README.txt",
      ]),
    ],
    ["cs-20261025T120000Z-abcdef.delta"],
  );
});

test("reviewBadgeTooltip: no badge at zero, the count otherwise", () => {
  assert.equal(reviewBadgeTooltip(0), undefined);
  assert.equal(reviewBadgeTooltip(1), "1 change set question waiting");
  assert.equal(reviewBadgeTooltip(3), "3 change set questions waiting");
});

test("parseTabAnswer: only an approve or a reject is an answer, and the message names no question", () => {
  assert.deepEqual(parseTabAnswer({ decision: "approve", digest: PYTHON_DIGEST }), {
    decision: "approve",
    digest: PYTHON_DIGEST,
  });
  assert.deepEqual(parseTabAnswer({ decision: "reject" }), {
    decision: "reject",
    digest: undefined,
  });
  for (const message of [undefined, null, "approve", [], { decision: "yes" }, { digest: "x" }]) {
    assert.equal(parseTabAnswer(message), undefined);
  }
  // An id or a question in the message is not read.
  const answer = parseTabAnswer({
    decision: "approve",
    digest: PYTHON_DIGEST,
    change_set_id: "cs-20261026T120000Z-ffffff",
    question: "safety",
  });
  assert.deepEqual(Object.keys(answer ?? {}).sort(), ["decision", "digest"]);
});

test("answerPlan: the tab's own question, re-read from disk, decides what is written", () => {
  const shown = record();
  // Approve with all three digests equal: written with the record's digest.
  assert.deepEqual(answerPlan(shown, shown, { decision: "approve", digest: PYTHON_DIGEST }), {
    ok: true,
    record: shown,
    decision: "approve",
    digest: PYTHON_DIGEST,
  });
  // Reject needs no digest check and carries the record's digest.
  assert.deepEqual(answerPlan(shown, shown, { decision: "reject", digest: "junk" }), {
    ok: true,
    record: shown,
    decision: "reject",
    digest: PYTHON_DIGEST,
  });
  // The record is gone, or the file now holds another question: nothing is written.
  for (const fresh of [undefined, record({ question: "safety" })]) {
    assert.deepEqual(answerPlan(shown, fresh, { decision: "approve", digest: PYTHON_DIGEST }), {
      ok: false,
      reason: NO_LONGER_WAITING,
    });
    assert.deepEqual(answerPlan(shown, fresh, { decision: "reject", digest: undefined }), {
      ok: false,
      reason: NO_LONGER_WAITING,
    });
  }
  // A posted digest of another text, or a text the record no longer holds, is refused.
  assert.equal(
    answerPlan(shown, shown, { decision: "approve", digest: textDigest("x") }).ok,
    false,
  );
  const replaced = withText("another question\n");
  assert.equal(
    answerPlan(shown, replaced, { decision: "approve", digest: PYTHON_DIGEST }).ok,
    false,
  );
});

test("answerPlan: another wait's record under the tab's name is refused, its digest equal", () => {
  // Same key, same text and digest, another project's server, another second:
  // the tab never showed this record's question, so neither button answers it.
  const shown = record();
  const others = [
    record({ project: { name: "Other", manifest: "/Users/dev/q/swcraft.yaml" } }),
    record({ created: "2026-10-26T12:00:01Z" }),
    record({ baseline: "sha256:0000" }),
    // Every field the same but the wait token: a later wait of the same
    // project and text in the same second is still another wait.
    record({ wait: OTHER_WAIT }),
  ];
  for (const fresh of others) {
    assert.equal(fresh.digest, shown.digest);
    for (const decision of ["approve", "reject"] as const) {
      assert.deepEqual(answerPlan(shown, fresh, { decision, digest: PYTHON_DIGEST }), {
        ok: false,
        reason: NO_LONGER_WAITING,
      });
    }
  }
});

// --- readReviewEntry over an injected fs -----------------------------------

function entryFs(options: { stat?: () => number; read?: () => string }): {
  fs: EntryFs;
  calls: string[];
} {
  const calls: string[] = [];
  const fake = {
    statSync: (file: string) => {
      calls.push(`stat ${path.basename(file)}`);
      return { mtimeMs: (options.stat ?? (() => 1_000))() };
    },
    readFileSync: (file: string) => {
      calls.push(`read ${path.basename(file)}`);
      return (options.read ?? (() => "{}"))();
    },
  };
  return { fs: fake as unknown as EntryFs, calls };
}

test("readReviewEntry: the entry carries the text and the stat's mtime, stat first", () => {
  const { fs: fake, calls } = entryFs({ stat: () => 42_000, read: () => "the record" });
  assert.deepEqual(readReviewEntry(fake, "/reviews", "a.delta.pending.json"), {
    name: "a.delta.pending.json",
    text: "the record",
    mtimeMs: 42_000,
  });
  assert.deepEqual(calls, ["stat a.delta.pending.json", "read a.delta.pending.json"]);
});

test("readReviewEntry: a stat that fails is no entry, and nothing is read", () => {
  const { fs: fake, calls } = entryFs({
    stat: () => {
      throw Object.assign(new Error("gone"), { code: "ENOENT" });
    },
  });
  assert.equal(readReviewEntry(fake, "/reviews", "a.delta.pending.json"), undefined);
  assert.deepEqual(calls, ["stat a.delta.pending.json"]);
});

test("readReviewEntry: a read that fails after the stat is no entry", () => {
  const { fs: fake } = entryFs({
    read: () => {
      throw Object.assign(new Error("denied"), { code: "EACCES" });
    },
  });
  assert.equal(readReviewEntry(fake, "/reviews", "a.delta.pending.json"), undefined);
});

test("reviewHtml: the review tab's page is byte for byte the one before the import tab", () => {
  // The SHA-256 of reviewHtml() for this suite's own inputs, taken before the import tab reused the
  // review tab's page: the import tab's rules are its own style and leave the review tab's page as
  // it was.
  const digest = (html: string) => createHash("sha256").update(html, "utf8").digest("hex");
  const pages: [string, string][] = [
    [
      reviewHtml(record(), { nonce: "abc123", utcOffsetMinutes: 0 }),
      "b1524efc63b0d142dec60b67b9a48525b781ef488a55b78fc60ad609b0868129",
    ],
    [
      reviewHtml(record({ client: null }), { nonce: "abc123", utcOffsetMinutes: 90 }),
      "98231665aeb8f0019498117383c791fed7f71ee0153946205c0307c97da1c5ef",
    ],
    [
      reviewHtml(warningRecord(), { nonce: "abc123", utcOffsetMinutes: 0 }),
      "56d3e13043eee1742320a1df9f71d25fecd8031e859766cc608817041386688d",
    ],
  ];
  for (const [html, expected] of pages) {
    assert.equal(digest(html), expected);
  }
});

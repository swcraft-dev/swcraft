// The diagrams' VS Code tab, its pure half (diagramsTab.ts): the rewrite of a
// generated page for the webview, the bridge that implements the page's host
// contract, the tab's stylesheet and its theme mapping, the page's own blocks,
// the checks on what crosses the boundary (links, opens, files read), the order
// of a show and the history.
//
// The pages read here are the generator's own goldens
// (`packages/tools/tests/fixtures/diagrams_golden/diagrams/`), so the rewrite is tested on
// what `swcraft generate diagrams` writes, and the page script's hash is the
// committed `schemas/diagram-page-script.json` — the two agree only when both
// were generated from one `page.PAGE_SCRIPT`.
//
// What this file does NOT reach: the webview itself (whether VS Code honours the policy, the
// nonces and the theme variables as written; measured in VS Code 1.145.2 on macOS, the look
// checked by hand), the panel and the editor beside it (commands.ts), and the page script's
// behaviour (`packages/tools/tests/test_diagram_page_script.py`). The bridge runs here under a DOM
// stub, never a browser.
//
// No `vscode` import.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { type TestContext, test } from "node:test";
import * as vm from "node:vm";

import {
  acceptNavigate,
  acceptOpen,
  asciiLower,
  BRIDGE_SCRIPT,
  type BridgeState,
  besideColumn,
  chooseView,
  cspHash,
  DIAGRAM_EDITOR_COMMANDS,
  DIAGRAMS_TAB_TITLE,
  DIAGRAMS_TAB_VIEW_TYPE,
  DRAWN_MEMBER_GROUPS,
  followLink,
  GENERATE_DIAGRAMS_BUTTON,
  GENERATED_HEAD_PREFIX,
  GENERATED_META_TAGS,
  GENERATED_TAGS,
  generateArgument,
  generatedByAnotherReleaseLine,
  HISTORY_LIMIT,
  indexedStorageName,
  indexHeadings,
  lexicalStorageName,
  memberAtLine,
  memberLineOf,
  modelAllowlist,
  noDiagramsLine,
  notOnAnyDiagramLine,
  OTHER_RELEASE_LINE,
  OUT_OF_DATE_LINE,
  OUT_OF_DATE_MESSAGE,
  outlineGroups,
  outlineRoot,
  PAGE_SCRIPT_HASH_FILE,
  PALETTE_BODY,
  parseElementsBlock,
  parseModelBlock,
  parsePageScriptHash,
  parseTabMessage,
  pickItems,
  pickPlaceholder,
  planShow,
  readDiagramIndex,
  realPathIndex,
  refusalDocument,
  refusalLine,
  rereadsScriptHash,
  resolveViewsFile,
  revealName,
  SHOW_IN_DIAGRAM_NO_ELEMENT_LINE,
  scriptJson,
  showInDiagramArgument,
  TAB_STYLE,
  TabHistory,
  TabState,
  tabCsp,
  tabTitle,
  UNREADABLE_PAGE_LINE,
  viewsForElement,
  webviewDocument,
  webviewDocumentRereading,
} from "./diagramsTab";
import {
  compoundBorder,
  compoundFill,
  compoundTitle,
  LIKEC4_CANVAS,
  LIKEC4_GRAY_RELATIONSHIP,
  LIKEC4_PRIMARY,
  LIKEC4_VERSION,
  type Likec4Role,
  likec4Palette,
  likec4Variable,
  MARK_WIDTH,
  MARKS,
  MEMBER_PORT_LABEL,
  PORT_LABEL,
  PORT_LIT_WIDTH,
  portFill,
  type Scheme,
  TAILWIND_V3,
} from "./likec4Palette";
import { loadReferences } from "./navigationCore";
import { checkStorageName, resolveModelFile } from "./storageName";
import { type OutlineSymbol, outlineOf } from "./symbolCore";
import { PROJECT_ROOT, SCHEMAS_DIR } from "./testSupport";

const GOLDEN = path.join(
  PROJECT_ROOT,
  "..",
  "packages",
  "tools",
  "tests",
  "fixtures",
  "diagrams_golden",
  "diagrams",
);

function golden(name: string): string {
  return fs.readFileSync(path.join(GOLDEN, name), "utf8");
}

const HASH =
  parsePageScriptHash(fs.readFileSync(path.join(SCHEMAS_DIR, PAGE_SCRIPT_HASH_FILE), "utf8")) ?? "";
const NONCE = "bm9uY2Vub25jZW5vbmNlbm9u";
const STATE: BridgeState = {
  fragment: "",
  reveal: null,
  canBack: false,
  canForward: false,
  outOfDate: false,
};

function rewrite(html: string, state: BridgeState = STATE) {
  return webviewDocument(html, { nonce: NONCE, scriptHash: HASH, state });
}

function page(html: string): string {
  const document = rewrite(html);
  assert.equal(document.kind, "page", JSON.stringify(document));
  return document.kind === "page" ? document.html : "";
}

/** The text of the one executable script of *html* (a `<script>` with no attributes). */
function pageScriptText(html: string): string {
  return html.split("<script>")[1].split("</script>")[0];
}

const VIEW_PAGES = fs
  .readdirSync(GOLDEN)
  .filter((name) => name.endsWith(".html") && name !== "index.html");

// --- the page script's hash ------------------------------------------------------

test("the committed page-script hash is the CSP hash of every golden view page's script", () => {
  assert.match(HASH, /^[A-Za-z0-9+/]{43}=$/);
  assert.ok(VIEW_PAGES.length >= 5, "the goldens hold the view pages");
  for (const name of VIEW_PAGES) {
    assert.equal(cspHash(pageScriptText(golden(name))), HASH, name);
  }
});

test("parsePageScriptHash takes only the generated file's shape", () => {
  assert.equal(parsePageScriptHash(`{"sha256": "${HASH}"}\n`), HASH);
  for (const text of [
    "",
    "not json",
    "[]",
    "null",
    '{"sha256": 1}',
    '{"sha256": "short="}',
    '{"sha": "x"}',
  ]) {
    assert.equal(parsePageScriptHash(text), undefined, text);
  }
});

// --- the rewrite --------------------------------------------------------------------

test("webviewDocument: the CSP is the first child of <head>, with the nonce and the page script's hash", () => {
  for (const name of [...VIEW_PAGES, "index.html"]) {
    const html = page(golden(name));
    const afterHead = html.slice(html.indexOf("<head>") + "<head>".length);
    assert.ok(
      afterHead.startsWith(
        `\n<meta http-equiv="Content-Security-Policy" content="${tabCsp(NONCE, HASH)}">`,
      ),
      name,
    );
    assert.equal(html.split("Content-Security-Policy").length, 2, `${name}: one policy`);
  }
  assert.equal(
    tabCsp("N", "P"),
    "default-src 'none'; style-src 'nonce-N'; script-src 'nonce-N' 'sha256-P'; form-action 'none'; base-uri 'none'",
  );
});

test("webviewDocument: every <style> is nonced — the page's, the SVG's and the tab's — and nothing else is", () => {
  for (const name of [...VIEW_PAGES, "index.html"]) {
    const original = golden(name);
    const html = page(original);
    const styles = html.match(/<style\b[^>]*>/g) ?? [];
    assert.equal(
      styles.length,
      (original.match(/<style\b/g) ?? []).length + 1,
      `${name}: the tab's style added`,
    );
    for (const tag of styles) {
      assert.equal(tag.startsWith(`<style nonce="${NONCE}"`), true, `${name}: ${tag}`);
    }
  }
  // The SVG's own style is nonced in place.
  assert.ok(page(golden("ibd-Chassis.html")).includes(`<style nonce="${NONCE}">g.edges use{`));
});

test("webviewDocument: the JSON blocks are data and come through untouched", () => {
  for (const name of [...VIEW_PAGES, "index.html"]) {
    const original = golden(name);
    const html = page(original);
    const blocks =
      original.match(/<script type="application\/json"[^>]*>[\s\S]*?<\/script>/g) ?? [];
    assert.ok(blocks.length > 0, name);
    for (const block of blocks) {
      assert.ok(html.includes(block), `${name}: ${block.slice(0, 60)}`);
    }
  }
});

test("webviewDocument: a name whose lowercase is longer (İ) moves no offset — the hash holds and the blocks parse", () => {
  // `"İ".toLowerCase()` is two code units: an offset found in a lowered copy would
  // land one unit late per İ before it. ẞ and astral letters ride along.
  assert.equal("\u0130".toLowerCase().length, 2, "the premise: İ lowers to two code units");
  assert.equal(asciiLower("İ ẞ 𝔘 ABC"), "İ ẞ 𝔘 abc");
  const odd = "\u0130stanbul \u0130\u0130 \u1E9E \u{1D518}\u{1D52B}";
  for (const name of [...VIEW_PAGES, "index.html"]) {
    const original = golden(name);
    const named = original
      .split("Diagram fixture")
      .join(`${odd} Diagram fixture`)
      .replace(
        '{"0": {"file": "compositions/Chassis.yaml"',
        `{"0": {"file": "compositions/${odd}.yaml"`,
      );
    assert.notEqual(named, original, name);
    const html = page(named);
    const blocks = named.match(/<script type="application\/json"[^>]*>[\s\S]*?<\/script>/g) ?? [];
    for (const block of blocks) {
      assert.ok(html.includes(block), `${name}: ${block.slice(0, 60)}`);
    }
    if (name === "index.html") {
      assert.ok(html.includes(`;</script>\n</body>`), "index: the bridge right before </body>");
      const elements = parseElementsBlock(html);
      assert.equal(elements.kind, "ok", JSON.stringify(elements));
      assert.equal(parseElementsBlock(named).kind, "ok");
    } else {
      assert.equal(pageScriptText(html), pageScriptText(original), name);
      const keys = (read: ReturnType<typeof parseModelBlock>) =>
        read.kind === "ok" ? [...read.value.keys()] : read.kind;
      assert.deepEqual(keys(parseModelBlock(html)), keys(parseModelBlock(original)), name);
    }
  }
  const chassis = parseModelBlock(
    golden("ibd-Chassis.html")
      .split("Diagram fixture")
      .join(odd)
      .replace(
        '{"0": {"file": "compositions/Chassis.yaml"',
        `{"0": {"file": "compositions/${odd}.yaml"`,
      ),
  );
  assert.equal(chassis.kind, "ok");
  assert.deepEqual(chassis.kind === "ok" ? chassis.value.get("0") : undefined, {
    file: `compositions/${odd}.yaml`,
    member: null,
  });
});

test("webviewDocument: the bridge comes before the page's script, which keeps its text and its hash", () => {
  for (const name of VIEW_PAGES) {
    const original = golden(name);
    const html = page(original);
    const bridge = html.indexOf(`<script nonce="${NONCE}">${BRIDGE_SCRIPT}(`);
    const script = html.indexOf("<script>");
    assert.ok(bridge !== -1 && bridge < script, name);
    assert.equal(pageScriptText(html), pageScriptText(original), name);
    assert.equal(cspHash(pageScriptText(html)), HASH);
    assert.equal(
      html.match(/<script>/g)?.length,
      1,
      `${name}: the page script stays unnonced, allowed by its hash`,
    );
  }
});

test("webviewDocument: on the index, which has no script, the bridge is the last child of <body>", () => {
  const html = page(golden("index.html"));
  const bridge = html.indexOf(`<script nonce="${NONCE}">${BRIDGE_SCRIPT}(`);
  assert.ok(bridge !== -1);
  assert.match(html.slice(bridge), /^<script nonce="[^"]+">[\s\S]*?<\/script>\n<\/body>/);
});

test("webviewDocument: the tab's stylesheet is the last child of <head>, after the page's own style", () => {
  for (const name of [...VIEW_PAGES, "index.html"]) {
    const html = page(golden(name));
    const head = html.slice(0, html.indexOf("</head>"));
    assert.ok(head.endsWith(`<style nonce="${NONCE}">\n${TAB_STYLE}</style>\n`), name);
    assert.ok(
      head.indexOf(TAB_STYLE) > head.lastIndexOf("</style>", head.length - TAB_STYLE.length - 10),
      name,
    );
  }
});

test("webviewDocument: the bridge's state is a JSON literal no markup can break out of", () => {
  const state: BridgeState = {
    fragment: "</script><script>alert(1)</script>",
    reveal: { file: "swc/a/S.yaml", member: null, name: "a\u2028b & <c>" },
    canBack: true,
    canForward: false,
    outOfDate: true,
  };
  const document = rewrite(golden("ibd-Chassis.html"), state);
  assert.equal(document.kind, "page");
  const html = document.kind === "page" ? document.html : "";
  assert.equal(html.includes("alert(1)</script>"), false);
  assert.ok(html.includes(`${BRIDGE_SCRIPT}(${scriptJson(state)});`));
  assert.deepEqual(JSON.parse(scriptJson(state)), state);
  assert.equal(/[<>&\u2028\u2029]/.test(scriptJson(state)), false);
});

test("webviewDocument refuses a page whose script is not this release's: another release, a replaced script", () => {
  const original = golden("ibd-Chassis.html");
  const replaced = original.replace(
    "<script>(function () {",
    "<script>(function () { window.x = 1;",
  );
  assert.notEqual(replaced, original);
  for (const document of [
    rewrite(replaced),
    webviewDocument(original, {
      nonce: NONCE,
      scriptHash: cspHash("another release's script"),
      state: STATE,
    }),
  ]) {
    assert.deepEqual(document, {
      kind: "refused",
      reason: "other-release",
      line: OTHER_RELEASE_LINE,
    });
  }
});

test("webviewDocumentRereading: a stale cached hash is read afresh once, the page shown and the fresh hash kept", () => {
  const original = golden("ibd-Chassis.html");
  const stale = cspHash("the release installed before this one");
  let reads = 0;
  const answer = webviewDocumentRereading(
    original,
    { nonce: NONCE, scriptHash: stale, state: STATE },
    () => {
      reads += 1;
      return HASH;
    },
  );
  assert.equal(reads, 1);
  assert.equal(answer.scriptHash, HASH, "the cache takes the fresh hash");
  assert.deepEqual(answer.document, rewrite(original));
  assert.equal(answer.document?.kind, "page");
});

test("webviewDocumentRereading: a file that still differs refuses after one re-read, never a loop", () => {
  const original = golden("ibd-Chassis.html");
  const stale = cspHash("the release installed before this one");
  const other = cspHash("yet another release");
  for (const fresh of [stale, other]) {
    let reads = 0;
    const answer = webviewDocumentRereading(
      original,
      { nonce: NONCE, scriptHash: stale, state: STATE },
      () => {
        reads += 1;
        return fresh;
      },
    );
    assert.equal(reads, 1);
    assert.equal(answer.scriptHash, fresh);
    assert.deepEqual(answer.document, {
      kind: "refused",
      reason: "other-release",
      line: OTHER_RELEASE_LINE,
    });
  }
});

test("webviewDocumentRereading: a hash file gone at the re-read answers no document, which shows its own line", () => {
  const original = golden("ibd-Chassis.html");
  let reads = 0;
  const answer = webviewDocumentRereading(
    original,
    { nonce: NONCE, scriptHash: cspHash("stale"), state: STATE },
    () => {
      reads += 1;
      return undefined;
    },
  );
  assert.equal(reads, 1);
  assert.deepEqual(answer, { document: undefined, scriptHash: undefined });
});

test("webviewDocumentRereading: a shown page and the other refusals never re-read", () => {
  const original = golden("ibd-Chassis.html");
  const pages = [
    original,
    golden("index.html"),
    original.replace("<head>", "<nohead>").replace("</head>", "</nohead>"),
    original.replace("</body>", "<script>1</script></body>"),
    original.replace("<script>(function", '<script src="x.js">(function'),
  ];
  const reasons: Array<string | undefined> = [];
  for (const html of pages) {
    const answer = webviewDocumentRereading(
      html,
      { nonce: NONCE, scriptHash: HASH, state: STATE },
      () => assert.fail("no re-read"),
    );
    assert.equal(answer.scriptHash, HASH);
    assert.deepEqual(answer.document, rewrite(html));
    reasons.push(answer.document?.kind === "refused" ? answer.document.reason : undefined);
  }
  assert.deepEqual(reasons, [undefined, undefined, "no-head", "several-scripts", "foreign-tag"]);
  assert.equal(rereadsScriptHash({ kind: "page", html: "" }), false);
  for (const reason of ["no-head", "several-scripts", "foreign-tag"] as const) {
    assert.equal(rereadsScriptHash({ kind: "refused", reason, line: UNREADABLE_PAGE_LINE }), false);
  }
  assert.equal(
    rereadsScriptHash({ kind: "refused", reason: "other-release", line: OTHER_RELEASE_LINE }),
    true,
  );
});

test("webviewDocument refuses a page with no <head>, with two executable scripts, or with a script src or type", () => {
  const original = golden("ibd-Chassis.html");
  assert.deepEqual(
    rewrite(original.replace("<head>", "<nohead>").replace("</head>", "</nohead>")),
    {
      kind: "refused",
      reason: "no-head",
      line: UNREADABLE_PAGE_LINE,
    },
  );
  assert.equal((rewrite(original.replace("</head>", "")) as { reason?: string }).reason, "no-head");
  assert.equal(
    (rewrite(original.replace("</body>", "<script>1</script></body>")) as { reason?: string })
      .reason,
    "several-scripts",
  );
  assert.equal(
    (
      rewrite(original.replace("</body>", '<script type="module">1</script></body>')) as {
        reason?: string;
      }
    ).reason,
    "foreign-tag",
    "a start tag the generator never writes",
  );
  assert.equal(
    (
      rewrite(original.replace("<script>(function", '<script src="x.js">(function')) as {
        reason?: string;
      }
    ).reason,
    "foreign-tag",
  );
  const index = golden("index.html");
  assert.equal(
    (
      rewrite(index.replace("</body>", '<script src="x.js"></script></body>')) as {
        reason?: string;
      }
    ).reason,
    "foreign-tag",
  );
  assert.throws(() =>
    webviewDocument(original, { nonce: 'x" onload="', scriptHash: HASH, state: STATE }),
  );
});

test("webviewDocument: the policy is only ever placed in the real head — a page not opening with the generator's head is refused", () => {
  const original = golden("ibd-Chassis.html");
  const index = golden("index.html");
  assert.ok(original.startsWith(GENERATED_HEAD_PREFIX) && index.startsWith(GENERATED_HEAD_PREFIX));
  const refused = { kind: "refused", reason: "no-head", line: UNREADABLE_PAGE_LINE };
  // (a) a comment holding a <head> before the real one: the policy would land in the comment.
  assert.deepEqual(
    rewrite(original.replace("<!DOCTYPE html>\n", "<!DOCTYPE html><!-- <head></head> -->\n")),
    refused,
  );
  // (b) body content before a literal <head>: Chromium ignores a policy outside the real head.
  assert.deepEqual(
    rewrite(
      `<html><body><img src=x onerror="acquireVsCodeApi()"><head></head>${original.slice(GENERATED_HEAD_PREFIX.length)}`,
    ),
    refused,
  );
  // Any other opening: a leading byte-order mark, another language, CRLF line ends, upper case.
  for (const variant of [
    `\ufeff${original}`,
    original.replace('<html lang="en">', '<html lang="de">'),
    original.replace(/\n/g, "\r\n"),
    original.replace("<head>", "<HEAD>"),
  ]) {
    assert.equal((rewrite(variant) as { reason?: string }).reason, "no-head");
  }
});

test("webviewDocument: the external review's bypass — a script taken for a JSON block, nonced through a <style> in its attributes — is refused", () => {
  const payload = '<script data-type="application/json" x=<style>alert(1)</script>';
  for (const html of [golden("ibd-Chassis.html"), golden("index.html")]) {
    for (const at of ["</head>", "</body>"]) {
      const document = rewrite(html.replace(at, `${payload}${at}`));
      assert.deepEqual(
        document,
        { kind: "refused", reason: "foreign-tag", line: UNREADABLE_PAGE_LINE },
        at,
      );
    }
  }
});

test("webviewDocument: any <script or <style token not exactly a generator's start tag is refused, in any case, anywhere", () => {
  const view = golden("ibd-Chassis.html");
  const index = golden("index.html");
  const variants = [
    // The JSON type spelt any way but the generator's.
    "<script type='application/json' id=\"swcraft-up\">alert(1)</script>",
    "<script type=application/json id=swcraft-up>alert(1)</script>",
    '<script TYPE="application/json" id="swcraft-up">alert(1)</script>',
    '<script type="application/json" type="module" id="swcraft-up">alert(1)</script>',
    '<script type="module" type="application/json" id="swcraft-up">alert(1)</script>',
    '<script type="application/json" id="swcraft-other">{}</script>',
    '<script type="application/json">{}</script>',
    '<SCRIPT type="application/json" id="swcraft-up">{}</SCRIPT>',
    '<script\ttype="application/json" id="swcraft-up">{}</script>',
    '<script/type="application/json" id="swcraft-up">{}</script>',
    // A nested token in another tag's attributes.
    '<div title="<style nonce=x>"></div>',
    "<p class='<script src=x.js>'></p>",
    '<a href="#" data-x=<style/>x</a>',
    // Any attribute on a style, or an upper-case one.
    '<style media="all">p{}</style>',
    "<STYLE>p{}</STYLE>",
    '<style nonce="guess">p{}</style>',
    // A token in a comment, or a tag name the token begins.
    "<!-- <script src=x.js> -->",
    "<styles></styles>",
  ];
  for (const variant of variants) {
    for (const html of [view, index]) {
      for (const at of ["</head>", "</body>"]) {
        assert.deepEqual(
          rewrite(html.replace(at, `${variant}${at}`)),
          { kind: "refused", reason: "foreign-tag", line: UNREADABLE_PAGE_LINE },
          `${variant} before ${at}`,
        );
      }
    }
  }
  // A token inside the page script's own text is a token too (and the hash would differ anyway).
  assert.equal(
    (
      rewrite(
        view.replace("<script>(function", "<script>(function(){'<style x>'})\n(function"),
      ) as {
        reason?: string;
      }
    ).reason,
    "foreign-tag",
  );
});

test("webviewDocument: a nonce goes only into the generator's own start tags — never into a script's attributes", () => {
  // An exact <style> inside an attribute value is nonced in place (a stated limit), and the only
  // script start tags are the exact generated ones, so no script can carry the nonce.
  const view = golden("ibd-Chassis.html");
  const html = page(view.replace("</body>", '<div title="<style>"></div></body>'));
  for (const tag of html.match(/<script\b[^>]*>/gi) ?? []) {
    assert.ok(
      tag === "<script>" ||
        tag === `<script nonce="${NONCE}">` ||
        /^<script type="application\/json" id="swcraft-[a-z]+">$/.test(tag),
      tag,
    );
  }
  assert.ok(html.includes(`<div title="<style nonce="${NONCE}">"></div>`));
});

test("every golden page of every fixture model passes the rewrite (the allowlist is the generator's exact tags)", () => {
  const root = path.dirname(GOLDEN);
  const pages: string[] = [];
  const walk = (directory: string) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const full = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        walk(full);
      } else if (entry.name.endsWith(".html")) {
        pages.push(full);
      }
    }
  };
  walk(root);
  assert.ok(pages.length >= 25, `${pages.length} golden pages`);
  for (const file of pages) {
    const document = rewrite(fs.readFileSync(file, "utf8"));
    assert.equal(
      document.kind,
      "page",
      `${path.relative(root, file)}: ${JSON.stringify(document)}`,
    );
  }
  for (const tag of GENERATED_TAGS) {
    assert.ok(
      pages.some((file) => fs.readFileSync(file, "utf8").includes(tag)),
      tag,
    );
  }
});

test("webviewDocument refuses any <meta> that is not the generator's exact text: a refresh, a second policy, a quoted >", () => {
  for (const html of [golden("ibd-Chassis.html"), golden("index.html")]) {
    for (const meta of [
      '<meta http-equiv="refresh" content="0;url=https://example.invalid/">',
      "<META HTTP-EQUIV=refresh content=0>",
      '<meta content="default-src *" http-equiv="Content-Security-Policy">',
      // a `>` inside a quoted value before http-equiv
      '<meta name="a>" http-equiv="refresh" content="0;url=https://example.invalid">',
      "<meta name='a>' http-equiv='refresh' content='0;url=https://example.invalid'>",
      '<META NAME="a>" HTTP-EQUIV="refresh" CONTENT="0">',
      // the generator's own text, then more attributes
      '<meta charset="utf-8" http-equiv="refresh" content="0">',
      '<meta charset="utf-8"\nhttp-equiv="refresh" content="0">',
      '<!-- <meta http-equiv="refresh" content="0"> -->',
      '<meta name="robots" content="none">',
      "<meta/http-equiv=refresh content=0>",
    ]) {
      for (const at of ["</head>", "</body>"]) {
        assert.deepEqual(
          rewrite(html.replace(at, `${meta}${at}`)),
          {
            kind: "refused",
            reason: "foreign-meta",
            line: UNREADABLE_PAGE_LINE,
          },
          meta,
        );
      }
    }
    // the generator's two, as written, pass; `<metadata` (an SVG element) is no meta token
    for (const tag of GENERATED_META_TAGS) {
      assert.ok(html.includes(tag), tag);
    }
    assert.equal(
      rewrite(html.replace("</body>", "<svg><metadata></metadata></svg></body>")).kind,
      "page",
    );
  }
  assert.equal(
    rereadsScriptHash({ kind: "refused", reason: "foreign-meta", line: UNREADABLE_PAGE_LINE }),
    false,
  );
});

test("webviewDocument refuses any tag the generator never writes that could move or load a frame", () => {
  for (const html of [golden("ibd-Chassis.html"), golden("index.html")]) {
    for (const markup of [
      '<base href="https://example.invalid/">',
      '<BASE HREF="https://example.invalid/">',
      '<link rel="stylesheet" href="https://example.invalid/a.css">',
      '<link rel="prefetch" href="https://example.invalid/">',
      '<iframe src="data:text/html,x"></iframe>',
      '<frameset><frame src="https://example.invalid/"></frameset>',
      '<object data="https://example.invalid/"></object>',
      '<embed src="https://example.invalid/">',
      '<form action="https://example.invalid/"><button>x</button></form>',
      '<!-- <base href="https://example.invalid/"> -->',
      '<g data-x="<base href=x>"></g>',
    ]) {
      for (const at of ["</head>", "</body>"]) {
        assert.deepEqual(
          rewrite(html.replace(at, `${markup}${at}`)),
          {
            kind: "refused",
            reason: "foreign-tag",
            line: UNREADABLE_PAGE_LINE,
          },
          markup,
        );
      }
    }
  }
});

test("refusalDocument: the one line and Generate, its own policy, no page text", () => {
  const html = refusalDocument(NONCE, OTHER_RELEASE_LINE);
  assert.ok(
    html.includes(
      `${OTHER_RELEASE_LINE} <button type="button" id="swcraft-generate">Generate</button>`,
    ),
  );
  assert.ok(html.includes(`script-src 'nonce-${NONCE}'`));
  assert.equal(html.includes("sha256-"), false);
  assert.ok(html.includes('postMessage({ type: "generate" })'));
  assert.ok(refusalDocument(NONCE, "<b>").includes("&lt;b&gt;"));
  assert.throws(() => refusalDocument("bad nonce!", "x"));
});

// --- the bridge, under a DOM stub ------------------------------------------------------

interface StubElement {
  tag: string;
  id: string;
  type: string;
  title: string;
  disabled: boolean;
  textContent: string;
  children: StubElement[];
  listeners: Record<string, Array<(event: unknown) => void>>;
  attributes: Record<string, string>;
  appendChild(child: StubElement): StubElement;
  insertBefore(child: StubElement, before: StubElement | undefined): StubElement;
  addEventListener(type: string, listener: (event: unknown) => void): void;
  setAttribute(name: string, value: string): void;
  getAttribute(name: string): string | null;
  getAttributeNS(namespace: string, name: string): string | null;
  closest(selector: string): StubElement | null;
  firstChild: StubElement | undefined;
  parentNode: StubElement | undefined;
}

function element(tag: string, attributes: Record<string, string> = {}): StubElement {
  const self: StubElement = {
    tag,
    id: "",
    type: "",
    title: "",
    disabled: false,
    textContent: "",
    children: [],
    listeners: {},
    attributes,
    parentNode: undefined,
    appendChild(child) {
      self.children.push(child);
      child.parentNode = self;
      return child;
    },
    insertBefore(child, before) {
      const at = before === undefined ? self.children.length : self.children.indexOf(before);
      self.children.splice(at < 0 ? self.children.length : at, 0, child);
      child.parentNode = self;
      return child;
    },
    addEventListener(type, listener) {
      self.listeners[type] = [...(self.listeners[type] ?? []), listener];
    },
    setAttribute(name, value) {
      self.attributes[name] = value;
    },
    getAttribute(name) {
      return Object.hasOwn(self.attributes, name) ? self.attributes[name] : null;
    },
    // A parsed SVG `xlink:href` is the attribute `href` in the XLink namespace.
    getAttributeNS(namespace, name) {
      return namespace === "http://www.w3.org/1999/xlink" && name === "href"
        ? self.getAttribute("xlink:href")
        : null;
    },
    // The DOM's closest(): this element or its nearest ancestor matching (only `a, area` is read).
    closest(selector) {
      for (let at: StubElement | undefined = self; at !== undefined; at = at.parentNode) {
        if (selector === "a, area" && (at.tag === "a" || at.tag === "area")) {
          return at;
        }
      }
      return null;
    },
    get firstChild() {
      return self.children[0];
    },
  };
  return self;
}

function runBridge(state: BridgeState, options: { header: boolean }) {
  const posted: unknown[] = [];
  const documentListeners: Array<{
    type: string;
    listener: (event: unknown) => void;
    capture: unknown;
  }> = [];
  const windowListeners: Record<string, Array<(event: unknown) => void>> = {};
  const header = element("header");
  const body = element("body");
  if (options.header) {
    body.appendChild(header);
  }
  const document = {
    body,
    createElement: (tag: string) => element(tag),
    createTextNode: (text: string) => ({ ...element("#text"), textContent: text }),
    querySelector: (selector: string) => (selector === "header" && options.header ? header : null),
    addEventListener: (type: string, listener: (event: unknown) => void, capture: unknown) => {
      documentListeners.push({ type, listener, capture });
    },
  };
  const context: Record<string, unknown> = {
    document,
    acquireVsCodeApi: () => ({ postMessage: (message: unknown) => posted.push(message) }),
  };
  context.window = {
    addEventListener: (type: string, listener: (event: unknown) => void) => {
      windowListeners[type] = [...(windowListeners[type] ?? []), listener];
    },
  };
  vm.runInNewContext(`${BRIDGE_SCRIPT}(${scriptJson(state)});`, context);
  const host = (context.window as Record<string, unknown>).swcraftHost as {
    state: { fragment: string; reveal: unknown };
    navigate(href: string): void;
    open(file: string, member: string | null): void;
    reload(): void;
    on(type: string, handler: (message: unknown) => void): void;
  };
  return { posted, documentListeners, windowListeners, header, body, host };
}

function click(
  listeners: ReturnType<typeof runBridge>["documentListeners"],
  type: string,
  target: StubElement,
) {
  const event = {
    target,
    prevented: false,
    stopped: false,
    preventDefault() {
      event.prevented = true;
    },
    stopPropagation() {
      event.stopped = true;
    },
  };
  for (const entry of listeners.filter((item) => item.type === type)) {
    entry.listener(event);
  }
  return event;
}

test("the bridge defines window.swcraftHost exactly as the page's contract states it", () => {
  const reveal = { file: "swc/chassis/Sensor.yaml", member: "ports/pSpeed", name: "Sensor" };
  const run = runBridge({ ...STATE, fragment: "Chassis", reveal }, { header: true });
  assert.deepEqual(Object.keys(run.host).sort(), ["navigate", "on", "open", "reload", "state"]);
  assert.deepEqual(JSON.parse(JSON.stringify(run.host.state)), { fragment: "Chassis", reveal });
  run.host.navigate("ibd-Vehicle.html#Chassis");
  run.host.open("swc/chassis/Sensor.yaml", "ports/pSpeed");
  run.host.open("compositions/Chassis.yaml", null);
  run.host.reload();
  assert.deepEqual(JSON.parse(JSON.stringify(run.posted)), [
    { type: "navigate", href: "ibd-Vehicle.html#Chassis" },
    { type: "open", file: "swc/chassis/Sensor.yaml", member: "ports/pSpeed" },
    { type: "open", file: "compositions/Chassis.yaml", member: null },
    { type: "reload" },
  ]);
});

test("the bridge routes the extension's messages to the handlers `on` registered, by type", () => {
  const run = runBridge(STATE, { header: true });
  const received: unknown[] = [];
  run.host.on("reveal", (message) => received.push(message));
  const deliver = (data: unknown) => {
    for (const listener of run.windowListeners.message ?? []) {
      listener({ data });
    }
  };
  assert.equal(run.windowListeners.message?.length, 1, "one message listener routes every type");
  deliver({ type: "reveal", file: "a.yaml", member: null, name: "A" });
  deliver({ type: "other" });
  deliver({ type: "hasOwnProperty" });
  deliver(null);
  deliver("reveal");
  assert.deepEqual(JSON.parse(JSON.stringify(received)), [
    { type: "reveal", file: "a.yaml", member: null, name: "A" },
  ]);
});

test("the bridge keeps every link click in the tab, in the capture phase, and consumes a middle click", () => {
  const run = runBridge(STATE, { header: true });
  const clicks = run.documentListeners.filter((entry) => entry.type === "click");
  assert.equal(clicks.length, 1);
  assert.equal(clicks[0].capture, true, "captured before the page or VS Code sees it");
  const link = element("a", { href: "overview-Chassis.html" });
  const event = click(run.documentListeners, "click", link);
  assert.equal(event.prevented && event.stopped, true);
  // A click on an SVG link lands on a shape or a text inside the <a>, never on the <a> itself.
  const svgLink = element("a", { href: "ibd-Chassis.html#Sensor" });
  const inner = svgLink.appendChild(element("g")).appendChild(element("text"));
  const innerEvent = click(run.documentListeners, "click", inner);
  assert.equal(innerEvent.prevented && innerEvent.stopped, true, "a press inside the link");
  assert.deepEqual(JSON.parse(JSON.stringify(run.posted)), [
    { type: "navigate", href: "overview-Chassis.html" },
    { type: "navigate", href: "ibd-Chassis.html#Sensor" },
  ]);
  const plain = click(run.documentListeners, "click", element("rect"));
  assert.equal(plain.prevented || plain.stopped, false, "a click on the picture is the page's");
  const middle = click(run.documentListeners, "auxclick", inner);
  assert.equal(middle.prevented && middle.stopped, true);
  assert.equal(run.posted.length, 2, "a middle click opens nothing");
});

test("the bridge leaves no anchor to the webview: xlink:href, <area>, an external href and no href at all", () => {
  const run = runBridge(STATE, { header: true });
  // An SVG link written with xlink:href only — a replaced page's — goes through navigate.
  const xlink = element("a", { "xlink:href": "vscode://file/etc/passwd" });
  const shape = xlink.appendChild(element("g")).appendChild(element("rect"));
  const pressed = click(run.documentListeners, "click", shape);
  assert.equal(pressed.prevented && pressed.stopped, true, "a press inside an xlink:href link");
  // An external URL is never followed by default: the host's followLink refuses it.
  const external = click(
    run.documentListeners,
    "click",
    element("a", { href: "https://example.com/" }),
  );
  assert.equal(external.prevented && external.stopped, true);
  const area = click(run.documentListeners, "click", element("area", { href: "nesting.html" }));
  assert.equal(area.prevented && area.stopped, true);
  // An anchor with neither attribute is swallowed and posts nothing.
  const bare = click(run.documentListeners, "click", element("a", { target: "_blank" }));
  assert.equal(bare.prevented && bare.stopped, true);
  const middle = click(run.documentListeners, "auxclick", shape);
  assert.equal(middle.prevented && middle.stopped, true);
  assert.deepEqual(JSON.parse(JSON.stringify(run.posted)), [
    { type: "navigate", href: "vscode://file/etc/passwd" },
    { type: "navigate", href: "https://example.com/" },
    { type: "navigate", href: "nesting.html" },
  ]);
  for (const href of ["vscode://file/etc/passwd", "https://example.com/"]) {
    assert.equal(followLink(href, os.tmpdir(), ["index.html"]).kind, "refused", href);
  }
});

test("the bridge puts Back and Forward first in the header, enabled by the history, posting back and forward", () => {
  const run = runBridge({ ...STATE, canBack: true, canForward: false }, { header: true });
  const history = run.header.children[0];
  assert.equal(history.id, "swcraft-history");
  const [back, forward] = history.children;
  assert.deepEqual(
    [back.id, back.textContent, back.disabled],
    ["swcraft-back", "\u2190 Back", false],
  );
  assert.deepEqual(
    [forward.id, forward.textContent, forward.disabled],
    ["swcraft-forward", "Forward \u2192", true],
  );
  for (const listener of back.listeners.click) listener({});
  for (const listener of forward.listeners.click) listener({});
  assert.deepEqual(JSON.parse(JSON.stringify(run.posted)), [{ type: "back" }, { type: "forward" }]);
});

test("the bridge on the index, which has no header, puts the buttons in a bar of their own", () => {
  const run = runBridge(STATE, { header: false });
  assert.equal(run.body.children[0].id, "swcraft-bar");
  assert.equal(run.body.children[0].children[0].id, "swcraft-history");
});

test("the bridge shows the out-of-date line with Generate only when the views are out of date", () => {
  assert.equal(
    runBridge(STATE, { header: true }).body.children.some((child) => child.id === "swcraft-stale"),
    false,
  );
  const run = runBridge({ ...STATE, outOfDate: true }, { header: true });
  const stale = run.body.children[0];
  assert.equal(stale.id, "swcraft-stale");
  assert.equal(stale.children[0].textContent, `${OUT_OF_DATE_LINE} `);
  const generate = stale.children[1];
  assert.equal(generate.id, "swcraft-generate");
  for (const listener of generate.listeners.click) listener({});
  assert.deepEqual(JSON.parse(JSON.stringify(run.posted)), [{ type: "generate" }]);
});

test("the bridge shows and hides the out-of-date line in place on the extension's message", () => {
  const run = runBridge(STATE, { header: true });
  const deliver = (data: unknown) => {
    for (const listener of run.windowListeners.message ?? []) {
      listener({ data });
    }
  };
  const stale = () => run.body.children.find((child) => child.id === "swcraft-stale");
  deliver({ type: OUT_OF_DATE_MESSAGE, outOfDate: false });
  assert.equal(stale(), undefined, "nothing to hide on a page that was current");
  deliver({ type: OUT_OF_DATE_MESSAGE, outOfDate: true });
  const line = stale();
  assert.ok(line !== undefined, "the line is drawn when the views go out of date");
  assert.equal(run.body.children[0], line, "first in the body, as at load");
  assert.equal((line as unknown as { hidden: boolean }).hidden, false);
  deliver({ type: OUT_OF_DATE_MESSAGE, outOfDate: true });
  assert.equal(
    run.body.children.filter((child) => child.id === "swcraft-stale").length,
    1,
    "a second message draws no second line",
  );
  deliver({ type: OUT_OF_DATE_MESSAGE, outOfDate: false });
  assert.equal((line as unknown as { hidden: boolean }).hidden, true, "a press made them current");
  deliver({ type: OUT_OF_DATE_MESSAGE, outOfDate: "yes" });
  assert.equal((line as unknown as { hidden: boolean }).hidden, true, "only true shows it");
  const loaded = runBridge({ ...STATE, outOfDate: true }, { header: true });
  for (const listener of loaded.windowListeners.message ?? []) {
    listener({ data: { type: OUT_OF_DATE_MESSAGE, outOfDate: false } });
  }
  assert.equal(
    (loaded.body.children[0] as unknown as { hidden: boolean }).hidden,
    true,
    "the line drawn at load is hidden the same way",
  );
  assert.deepEqual(run.posted, [], "the message posts nothing back");
  assert.match(
    TAB_STYLE,
    /#swcraft-stale\[hidden\] \{ display: none; \}/,
    "the tab style honours hidden",
  );
});

// --- the tab stylesheet -----------------------------------------------------------------

/**
 * Every `--vscode-*` variable TAB_STYLE may name: each a colour id registered in
 * VS Code 1.145.2 (a `registerColor` call in the app's
 * `out/vs/workbench/workbench.desktop.main.js`; a registered id is injected into
 * a webview as `--vscode-<id, dots as dashes>`). Not reached: a later VS Code
 * that drops one of them.
 */
const REGISTERED_COLOUR_VARIABLES = new Set([
  "--vscode-editor-background", // editor.background
  "--vscode-editor-foreground", // editor.foreground
  "--vscode-descriptionForeground", // descriptionForeground
  "--vscode-editorWidget-background", // editorWidget.background
  "--vscode-editorWidget-foreground", // editorWidget.foreground
  "--vscode-editorWidget-border", // editorWidget.border
  "--vscode-widget-border", // widget.border
  "--vscode-widget-shadow", // widget.shadow
  "--vscode-editor-findMatchHighlightBackground", // editor.findMatchHighlightBackground
  "--vscode-editor-findMatchHighlightBorder", // editor.findMatchHighlightBorder
  "--vscode-editor-findMatchBorder", // editor.findMatchBorder
  "--vscode-charts-yellow", // charts.yellow
  "--vscode-charts-red", // charts.red
  "--vscode-focusBorder", // focusBorder
  "--vscode-contrastBorder", // contrastBorder
  "--vscode-textLink-foreground", // textLink.foreground
  "--vscode-textLink-activeForeground", // textLink.activeForeground
  "--vscode-textCodeBlock-background", // textCodeBlock.background
  "--vscode-button-background", // button.background
  "--vscode-button-foreground", // button.foreground
  "--vscode-button-hoverBackground", // button.hoverBackground
  "--vscode-button-border", // button.border
  "--vscode-button-secondaryBackground", // button.secondaryBackground
  "--vscode-button-secondaryForeground", // button.secondaryForeground
  "--vscode-button-secondaryHoverBackground", // button.secondaryHoverBackground
  "--vscode-input-background", // input.background
  "--vscode-input-foreground", // input.foreground
  "--vscode-input-border", // input.border
  "--vscode-input-placeholderForeground", // input.placeholderForeground
  "--vscode-editorWarning-foreground", // editorWarning.foreground
]);

/** CSS's named colours (CSS Color 4), every one a literal TAB_STYLE must not write. */
const NAMED_COLOURS = new Set(
  `aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown
  burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan
  darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred
  darksalmon darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink
  deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold
  goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender lavenderblush
  lawngreen lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey
  lightpink lightsalmon lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow
  lime limegreen linen magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen
  mediumslateblue mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin
  navajowhite navy oldlace olive olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise
  palevioletred papayawhip peachpuff peru pink plum powderblue purple rebeccapurple red rosybrown royalblue
  saddlebrown salmon sandybrown seagreen seashell sienna silver skyblue slateblue slategray slategrey snow
  springgreen steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke yellow yellowgreen`
    .split(/\s+/)
    .filter((name) => name !== ""),
);

interface CssRule {
  selectors: string[];
  declarations: Map<string, string>;
}

/** TAB_STYLE's rules: a flat sheet (no at-rule, no nesting), split at its braces. */
function rules(css: string): CssRule[] {
  const parsed: CssRule[] = [];
  for (const block of css.split("}")) {
    const open = block.indexOf("{");
    if (open === -1) {
      assert.equal(block.trim(), "", `a fragment outside any rule: ${block}`);
      continue;
    }
    const selectorText = block.slice(0, open).replace(/\s+/g, " ").trim();
    // Split the selector list at the top-level commas only (`:where(a, b)` keeps its own).
    const selectors: string[] = [];
    let depth = 0;
    let current = "";
    for (const character of selectorText) {
      if (character === "(") depth += 1;
      if (character === ")") depth -= 1;
      if (character === "," && depth === 0) {
        selectors.push(current.trim());
        current = "";
      } else {
        current += character;
      }
    }
    selectors.push(current.trim());
    const declarations = new Map<string, string>();
    for (const declaration of block.slice(open + 1).split(";")) {
      const colon = declaration.indexOf(":");
      if (colon !== -1) {
        declarations.set(
          declaration.slice(0, colon).trim(),
          declaration
            .slice(colon + 1)
            .replace(/\s+/g, " ")
            .trim(),
        );
      }
    }
    parsed.push({ selectors, declarations });
  }
  return parsed;
}

const TAB_RULES = rules(TAB_STYLE);

test("TAB_STYLE resets VS Code's padding on a view page's body and owns body's background and colour", () => {
  const padding = TAB_RULES.find((rule) => rule.selectors.includes("body[data-view]"));
  assert.equal(padding?.declarations.get("padding"), "0");
  const body = TAB_RULES.find(
    (rule) => rule.selectors.length === 1 && rule.selectors[0] === "body",
  );
  assert.equal(body?.declarations.get("background"), "var(--vscode-editor-background)");
  assert.equal(body?.declarations.get("color"), "var(--vscode-editor-foreground)");
});

/** The two rules that declare the LikeC4 palette's custom properties, light and dark. */
const PALETTE_DECLARATIONS = {
  "body.vscode-light:not(.vscode-high-contrast)": "light",
  "body.vscode-dark:not(.vscode-high-contrast)": "dark",
} as const;

test("TAB_STYLE names no colour literal but LikeC4's palette, declared once per scheme from likec4Palette.ts", () => {
  // The one admitted literal set is likec4Palette.ts' values (LikeC4 1.60.0, MIT, with two
  // changes), each a custom property of the light or the dark body. Nothing else is a literal.
  let sheet = TAB_STYLE;
  for (const [selector, scheme] of Object.entries(PALETTE_DECLARATIONS) as [string, Scheme][]) {
    const declared = TAB_RULES.filter(
      (rule) =>
        [...rule.declarations.values()].some((value) => !value.startsWith("var(")) &&
        rule.selectors.length === 1 &&
        rule.selectors[0] === selector,
    );
    assert.equal(declared.length, 1, selector);
    const palette = likec4Palette(scheme);
    const expected = new Map(
      (Object.keys(palette) as Likec4Role[]).map((name) => [likec4Variable(name), palette[name]]),
    );
    assert.deepEqual(declared[0].declarations, expected, selector);
    const text = `${selector} { ${[...expected].map(([name, value]) => `${name}: ${value};`).join(" ")} }`;
    assert.ok(sheet.includes(text), selector);
    sheet = sheet.replace(text, "");
  }
  // Every literal in the two rules is one of LikeC4's published constants.
  const published = new Set([
    ...Object.values(LIKEC4_PRIMARY),
    ...Object.values(LIKEC4_GRAY_RELATIONSHIP),
    ...Object.values(LIKEC4_CANVAS),
    ...Object.values(TAILWIND_V3),
  ]);
  for (const scheme of ["light", "dark"] as const) {
    for (const value of Object.values(likec4Palette(scheme))) {
      for (const hex of value.match(/#[0-9a-fA-F]{3,8}\b/g) ?? []) {
        assert.ok(published.has(hex), `${scheme}: ${hex}`);
      }
    }
  }
  const withoutVariables = sheet.replace(/--vscode-[A-Za-z0-9-]+/g, "");
  assert.equal(/#[0-9a-fA-F]{3,8}\b/.test(withoutVariables), false, "a hex colour");
  assert.equal(
    /\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix)\(/i.test(withoutVariables),
    false,
    "a colour function",
  );
  const words = withoutVariables.match(/[A-Za-z]+/g) ?? [];
  const named = words.filter((word) => NAMED_COLOURS.has(word.toLowerCase()));
  assert.deepEqual(named, [], "a named colour");
});

test("TAB_STYLE names only registered colour variables", () => {
  const named = new Set(TAB_STYLE.match(/--vscode-[A-Za-z0-9-]+/g) ?? []);
  assert.ok(named.size > 10);
  assert.deepEqual(
    [...named].filter((name) => !REGISTERED_COLOUR_VARIABLES.has(name)),
    [],
  );
});

test("TAB_STYLE has rules under each of VS Code's four body classes of a theme kind", () => {
  for (const selector of [
    "body.vscode-dark",
    "body.vscode-light",
    "body.vscode-high-contrast:not(.vscode-high-contrast-light)",
    "body.vscode-high-contrast-light",
  ]) {
    assert.ok(
      TAB_RULES.some((rule) => rule.selectors.some((text) => text.includes(selector))),
      selector,
    );
  }
});

test("TAB_STYLE never uses !important, and every rule that sets a stroke excludes the page's hit band", () => {
  // The page paints its `.hit-band` transparent with `!important` (page.py's
  // style); a theme rule may neither out-rank it nor reach it.
  assert.equal(TAB_STYLE.includes("!important"), false);
  const stroking = TAB_RULES.filter((rule) =>
    [...rule.declarations.keys()].some((name) => name.startsWith("stroke")),
  );
  assert.ok(stroking.length > 5);
  for (const rule of stroking) {
    for (const selector of rule.selectors) {
      assert.ok(
        selector.includes(":not(.hit-band)"),
        `a stroke rule reaching the band: ${selector}`,
      );
    }
  }
  // The page's band rule (page.py) holds its paint by !important, but not its
  // vector-effect or its pointer-events: a tab rule naming either would disable
  // the band in the tab alone.
  assert.equal(/pointer-events|vector-effect/.test(TAB_STYLE), false);
});

test("no stroke keeps its width at any zoom: strokes scale with the zoom, in every theme", () => {
  // No non-scaling strokes: on the large IBD at fit-zoom hundreds of connectors become one
  // light-grey mass.
  assert.equal(TAB_STYLE.includes("non-scaling"), false);
  assert.equal(
    TAB_RULES.some((rule) => rule.declarations.has("vector-effect")),
    false,
  );
});

/** The rules whose every selector carries *needle* and the mark class *mark* (`.hit` or `.current`). */
function markRules(needle: string, mark: string): CssRule[] {
  return TAB_RULES.filter(
    (rule) =>
      rule.selectors.every((selector) => selector.includes(needle)) &&
      rule.selectors.some((selector) => selector.includes(`svg ${mark} `)),
  );
}

test("the marks in the light and dark themes are Tailwind's amber and red, 4 px (his option 3, 2026-11-09)", () => {
  // The hit's fill stays the theme's find highlight, outside high contrast.
  const hitFill = markRules("body:not(.vscode-high-contrast)", ".hit").find((rule) =>
    rule.declarations.has("fill"),
  );
  assert.equal(
    hitFill?.declarations.get("fill"),
    "var(--vscode-editor-findMatchHighlightBackground)",
  );
  // The outlines: one rule each, every selector under the palette body.
  const selection = markRules(PALETTE_BODY, ".selected");
  const hit = markRules(PALETTE_BODY, ".hit");
  const current = markRules(PALETTE_BODY, ".current");
  for (const [rules, value] of [
    [selection, read("selection")],
    [hit, read("hit")],
    [current, read("current")],
  ] as const) {
    assert.equal(rules.length, 1, value);
    assert.deepEqual(
      rules[0].declarations,
      new Map([
        ["stroke", value],
        ["stroke-width", "4px"],
      ]),
    );
  }
  // The selection's rule names the hover and the pin's highlight too, and the lit
  // arrowheads take the same amber after the theme's own lit rule.
  for (const part of [".link:hover > rect", ".highlight path", ".selected > rect"]) {
    assert.ok(
      selection[0].selectors.some((selector) => selector.startsWith(`${PALETTE_BODY} svg ${part}`)),
      part,
    );
  }
  assert.equal(
    paletteRuleOf(ON('marker[id$="-lit"] path:not([fill="none"])')).declarations.get("fill"),
    read("selection"),
  );
  assert.ok(
    TAB_STYLE.indexOf(ON('marker[id$="-lit"] path:not([fill="none"])')) >
      TAB_STYLE.indexOf(':where(main > svg marker[id$="-lit"] path:not([fill="none"]))'),
  );
  // The order the theme's own marks keep: the selection, then the matches, the current last.
  const order = (rules: CssRule[]) => TAB_STYLE.indexOf(rules[0].selectors[0]);
  assert.ok(order(selection) < order(hit) && order(hit) < order(current));
  // The values: Tailwind CSS v3's default colours, per scheme.
  assert.deepEqual(MARKS, {
    light: { selection: "#f59e0b", current: "#dc2626", hit: "#fca5a5" },
    dark: { selection: "#fbbf24", current: "#f87171", hit: "#fca5a5" },
  });
  assert.equal(/--vscode-charts-(?:yellow|red)\b/.test(TAB_STYLE), false);
  assert.equal(/--vscode-editor-findMatchBackground\b/.test(TAB_STYLE), false);
});

test("the marks under both high-contrast kinds are the find-match borders at 3 px and 5 px", () => {
  // VS Code gives a high-contrast light body both classes, so one selector names both kinds.
  for (const kind of ["body.vscode-high-contrast"]) {
    const hit = TAB_RULES.find(
      (rule) =>
        rule.selectors.some((selector) => selector.startsWith(`${kind} svg .hit `)) &&
        rule.declarations.has("stroke"),
    );
    const current = TAB_RULES.find(
      (rule) =>
        rule.selectors.some((selector) => selector.startsWith(`${kind} svg .current `)) &&
        rule.declarations.has("stroke"),
    );
    assert.equal(
      hit?.declarations.get("stroke"),
      "var(--vscode-editor-findMatchHighlightBorder)",
      kind,
    );
    assert.equal(hit?.declarations.get("stroke-width"), "3px", kind);
    assert.equal(current?.declarations.get("stroke"), "var(--vscode-editor-findMatchBorder)", kind);
    assert.equal(current?.declarations.get("stroke-width"), "5px", kind);
  }
  // The fill and the yellow/red outlines never reach a high-contrast body.
  for (const rule of TAB_RULES) {
    const values = [...rule.declarations.values()].join(" ");
    if (/charts-yellow|charts-red|findMatchHighlightBackground/.test(values)) {
      for (const selector of rule.selectors) {
        assert.ok(selector.startsWith("body:not(.vscode-high-contrast)"), selector);
      }
    }
  }
});

test("the selection and the pin's highlight take the focus border; the widget fill reaches the dark side", () => {
  const selected = TAB_RULES.find((rule) =>
    rule.selectors.some((selector) => selector.startsWith("svg .selected >")),
  );
  const highlight = TAB_RULES.find((rule) =>
    rule.selectors.some((selector) => selector.startsWith("svg .highlight ")),
  );
  assert.equal(selected?.declarations.get("stroke"), "var(--vscode-focusBorder)");
  assert.equal(highlight?.declarations.get("stroke"), "var(--vscode-focusBorder)");
  const fills = TAB_RULES.filter(
    (rule) => rule.declarations.get("fill") === "var(--vscode-editorWidget-background)",
  );
  // The base mapping stands; in the dark theme the LikeC4 rules, later and of equal specificity,
  // take every box, label and port square from it, and what they do not name (a glyph, a count
  // square, an activation bar) keeps it.
  assert.equal(fills.length, 1);
  assert.deepEqual(fills[0].selectors, [
    ':where(body.vscode-dark main > svg, body.vscode-high-contrast:not(.vscode-high-contrast-light) main > svg) :where(rect, circle, path):where(:not([fill="none"]):not(.background):not(.hit-band):not(.port-hit))',
  ]);
  assert.ok(
    TAB_STYLE.indexOf("var(--vscode-editorWidget-background); }\n:where(main > svg marker") <
      TAB_STYLE.indexOf(ON("rect.background")),
    "the LikeC4 rules follow the dark side's widget fill",
  );
});

/** The palette rules: every rule whose declarations read a `--swcraft-likec4-*` property. */
const PALETTE_RULES = TAB_RULES.filter((rule) =>
  [...rule.declarations.values()].some((value) => value.includes("var(--swcraft-likec4-")),
);

/** The one palette rule one of whose selectors is *selector*, in full. */
function paletteRuleOf(selector: string): CssRule {
  const found = PALETTE_RULES.filter((rule) => rule.selectors.includes(selector));
  assert.equal(found.length, 1, selector);
  return found[0];
}

const read = (name: Likec4Role) => `var(${likec4Variable(name)})`;
const ON = (part: string, body = PALETTE_BODY) => `:where(${body} main > svg ${part})`;
const PACKED = ':is([data-view="nesting"], [data-view="deployment"])';

test("no palette rule reaches a high-contrast body: each is scoped to the light and dark themes", () => {
  // High contrast keeps the theme's own outlines and no palette value.
  assert.equal(PALETTE_BODY, "body:is(.vscode-light, .vscode-dark):not(.vscode-high-contrast)");
  assert.ok(PALETTE_RULES.length >= 10);
  for (const rule of PALETTE_RULES) {
    for (const selector of rule.selectors) {
      assert.ok(
        selector.startsWith(`:where(${PALETTE_BODY}`) ||
          selector.startsWith(`${PALETTE_BODY} svg .`) ||
          selector === `${PALETTE_BODY} main`,
        selector,
      );
    }
  }
  const declaring = TAB_RULES.filter((rule) =>
    [...rule.declarations.keys()].some((name) => name.startsWith("--swcraft-likec4-")),
  );
  assert.deepEqual(
    declaring.map((rule) => rule.selectors),
    [
      ["body.vscode-light:not(.vscode-high-contrast)"],
      ["body.vscode-dark:not(.vscode-high-contrast)"],
    ],
  );
  // The palette rules come before every mark, and are specificity 0 (`:where`).
  const sheetOrder = (needle: string) => TAB_STYLE.indexOf(needle);
  assert.ok(
    sheetOrder(`stroke: ${read("line")}; }\n:where(main > svg marker[id$="-lit"]`) > 0 ||
      sheetOrder(read("line")) < sheetOrder('marker[id$="-lit"]'),
    "a lit arrowhead stays the focus colour",
  );
  assert.ok(sheetOrder(read("compoundBorder")) < sheetOrder("svg .selected >"));
});

test("in the palette themes the whole diagram area is LikeC4's canvas; high contrast keeps the editor's", () => {
  const area = TAB_RULES.filter((rule) => rule.selectors.includes(`${PALETTE_BODY} main`));
  assert.equal(area.length, 1);
  assert.deepEqual(area[0].selectors, [`${PALETTE_BODY} main`]);
  assert.deepEqual(area[0].declarations, new Map([["background", read("canvas")]]));
  const plain = TAB_RULES.find(
    (rule) => rule.selectors.length === 1 && rule.selectors[0] === "main",
  );
  assert.equal(plain?.declarations.get("background"), "var(--vscode-editor-background)");
  assert.deepEqual(
    paletteRuleOf(ON("rect.background")).declarations,
    new Map([["fill", read("canvas")]]),
  );
});

test("the palette maps the SVG's marks onto LikeC4's compound, element and relationship", () => {
  // A compound (LikeC4's element drawn with its children inside): a frame, and in the
  // packed views every box holding boxes; solid: no dash anywhere.
  const compound = paletteRuleOf(ON('[data-role="frame"] > rect:not(.hit-band)'));
  assert.deepEqual(compound.selectors, [
    ON('[data-role="frame"] > rect:not(.hit-band)'),
    ON(
      '[data-role="member"]:not([data-kind="component"]) > rect:not(.hit-band)',
      `${PALETTE_BODY}${PACKED}`,
    ),
  ]);
  assert.deepEqual(
    compound.declarations,
    new Map([
      ["fill", read("compoundFill")],
      ["stroke", read("compoundBorder")],
      ["stroke-width", "3px"],
    ]),
  );
  const element = paletteRuleOf(ON('[data-kind="component"] > rect:not(.hit-band)'));
  assert.deepEqual(element.selectors, [
    ON('[data-kind="component"] > rect:not(.hit-band)'),
    ON('[data-role="member"] > rect:not(.hit-band)', `${PALETTE_BODY}:not(${PACKED})`),
    ON('[data-role="lifeline"] > rect:not(.hit-band)'),
  ]);
  assert.deepEqual(
    element.declarations,
    new Map([
      ["fill", read("fill")],
      ["stroke", read("stroke")],
    ]),
  );
  // The labels, told by the renderer's marks (data-kind, in-member), never by an anchor.
  const title = paletteRuleOf(ON("text.frame-label"));
  assert.deepEqual(title.selectors, [
    ON("text.frame-label"),
    ON('text.member-label:not([data-kind="component"])', `${PALETTE_BODY}${PACKED}`),
  ]);
  assert.equal(title.declarations.get("fill"), read("title"));
  const text = paletteRuleOf(ON('text.member-label[data-kind="component"]'));
  assert.deepEqual(text.selectors, [
    ON('text.member-label[data-kind="component"]'),
    ON("text.member-label", `${PALETTE_BODY}:not(${PACKED})`),
    ON("text.lifeline-label"),
  ]);
  assert.equal(text.declarations.get("fill"), read("text"));
  assert.equal(paletteRuleOf(ON("text.lifeline-detail")).declarations.get("fill"), read("detail"));
  assert.equal(paletteRuleOf(ON("text.port-label")).declarations.get("fill"), read("portLabel"));
  assert.equal(
    paletteRuleOf(ON("text.port-label.in-member")).declarations.get("fill"),
    read("memberPortLabel"),
  );
  assert.ok(
    TAB_STYLE.indexOf(ON("text.port-label.in-member")) > TAB_STYLE.indexOf(ON("text.port-label)")),
    "a member's port label: the later rule of equal specificity",
  );
  // Inside a member composition: the port label grey of the outside.
  const inComposition = paletteRuleOf(ON('text.port-label.in-member[data-kind="composition"]'));
  assert.deepEqual(inComposition.selectors, [
    ON('text.port-label.in-member[data-kind="composition"]'),
  ]);
  assert.deepEqual(inComposition.declarations, new Map([["fill", read("portLabel")]]));
  assert.ok(
    TAB_STYLE.indexOf(ON('text.port-label.in-member[data-kind="composition"]')) >
      TAB_STYLE.indexOf(ON("text.port-label.in-member)")),
    "a composition member's port label: after the in-member rule",
  );
  assert.ok(
    !TAB_STYLE.split("\n").some(
      (line) => line.includes('in-member[data-kind="composition"]') && !line.includes(PALETTE_BODY),
    ),
    "the composition member's port label rule reaches the palette themes only",
  );
  assert.equal(
    paletteRuleOf(ON("g.edge path:not(.hit-band)")).declarations.get("stroke"),
    read("line"),
  );
  assert.equal(
    paletteRuleOf(ON('marker path:not([fill="none"])')).declarations.get("fill"),
    read("line"),
  );
  assert.deepEqual(
    paletteRuleOf(ON(".port-box:not(.hit-band)")).declarations,
    new Map([
      ["fill", read("portFill")],
      ["stroke", read("line")],
    ]),
  );
  // The marks the selectors read are the ones the generator writes: every label of a box
  // in the golden SVGs carries a kind, and every kind is a compound or an element.
  const kinds = new Set<string>();
  for (const name of fs.readdirSync(GOLDEN).filter((file) => file.endsWith(".svg"))) {
    const svg = golden(name);
    for (const match of svg.matchAll(/data-kind="([a-z]+)"/g)) kinds.add(match[1]);
    for (const label of svg.matchAll(
      /<text class="(member-label|frame-label|lifeline-label)"[^>]*>/g,
    )) {
      assert.match(label[0], / data-kind="[a-z]+"/, `${name}: ${label[0]}`);
    }
  }
  assert.deepEqual([...kinds].sort(), [
    "component",
    "composition",
    "core",
    "partition",
    "processor",
    "sequence",
    "task",
  ]);
  assert.match(
    golden("ibd-Chassis.svg"),
    /<text class="port-label in-member"[^>]* data-kind="component"/,
  );
  assert.match(
    golden("ibd-Vehicle.svg"),
    /<text class="port-label in-member"[^>]* data-kind="composition"/,
  );
  assert.match(golden("ibd-Chassis.svg"), /<text class="port-label" /);
});

test("the LikeC4 constants are as published, and his two changes are the only departures", () => {
  assert.equal(LIKEC4_VERSION, "1.60.0");
  assert.deepEqual(
    { ...LIKEC4_PRIMARY },
    { fill: "#3b82f6", stroke: "#2563eb", hiContrast: "#eff6ff", loContrast: "#bfdbfe" },
  );
  assert.equal(LIKEC4_GRAY_RELATIONSHIP.line, "#8D8D8D");
  assert.equal(LIKEC4_GRAY_RELATIONSHIP.label, "#C9C9C9");
  assert.equal(LIKEC4_CANVAS.light, "#fff");
  assert.equal(LIKEC4_CANVAS.dark, "#242424");
  assert.equal(compoundFill("#3b82f6"), "oklch(from #3b82f6 l c h / 15%)");
  assert.equal(compoundBorder("#2563eb"), "oklch(from #2563eb l c h / 57.5%)");
  assert.equal(
    compoundTitle("#eff6ff", "#2563eb", "dark"),
    "oklch(from color-mix(in oklch, #eff6ff, #2563eb 60%) calc(l + .2) c h)",
  );
  // No border style is carried, and TAB_STYLE draws no dash outside high contrast; the light
  // member port label is the component's text colour.
  assert.equal(MEMBER_PORT_LABEL.light, LIKEC4_PRIMARY.hiContrast);
  assert.equal(MEMBER_PORT_LABEL.dark, PORT_LABEL.dark);
});

test("outside high contrast every mark is solid; high contrast keeps its dashed marks", () => {
  // The palette themes' marks are solid, never dashed.
  for (const rule of TAB_RULES) {
    if (rule.selectors.some((selector) => selector.startsWith("body.vscode-high-contrast")))
      continue;
    assert.equal(rule.declarations.has("stroke-dasharray"), false, rule.selectors.join(", "));
  }
  // High contrast: its own marks, untouched by the palette.
  const highContrast = "body.vscode-high-contrast svg";
  const markOf = (mark: string) =>
    TAB_RULES.filter((rule) =>
      rule.selectors.every((selector) => selector.startsWith(`${highContrast} ${mark}`)),
    );
  assert.deepEqual(
    markOf(".hit ").map((rule) => rule.declarations),
    [
      new Map([
        ["stroke", "var(--vscode-editor-findMatchHighlightBorder)"],
        ["stroke-width", "3px"],
      ]),
    ],
  );
  assert.deepEqual(
    markOf(".current ").map((rule) => rule.declarations),
    [
      new Map([
        ["stroke", "var(--vscode-editor-findMatchBorder)"],
        ["stroke-width", "5px"],
      ]),
    ],
  );
  assert.deepEqual(
    markOf(".selected >").map((rule) => rule.declarations),
    [
      new Map([
        ["stroke-width", "3px"],
        ["stroke-dasharray", "4 2"],
      ]),
    ],
  );
  // The only dash in the sheet is the high-contrast selection's.
  const dashed = TAB_RULES.filter((rule) => rule.declarations.has("stroke-dasharray"));
  assert.equal(dashed.length, 1);
});

// --- the port and composition marks -------------------------------------------------------

/** The palette themes' selection-mark rule of the lit and selected port. */
const PORT_MARK = TAB_RULES.filter((rule) =>
  rule.selectors.some((selector) => selector.includes("svg .port.highlight > rect.port-box")),
);

test("remark 2: one colour per port - the square's outline, the glyph's outline and the line", () => {
  const square = paletteRuleOf(ON(".port-box:not(.hit-band)"));
  assert.deepEqual(square.selectors, [
    ON(".port-box:not(.hit-band)"),
    ON('g.port g.glyph path:not([fill="none"]):not(.hit-band)'),
    ON("g.port g.glyph circle:not(.hit-band)"),
  ]);
  const open = paletteRuleOf(ON("g.port g.glyph line:not(.hit-band)"));
  assert.deepEqual(open.selectors, [
    ON('g.port g.glyph path[fill="none"]:not(.hit-band)'),
    ON("g.port g.glyph line:not(.hit-band)"),
  ]);
  assert.deepEqual(open.declarations, new Map([["stroke", read("line")]]));
  const edge = paletteRuleOf(ON("g.edge path:not(.hit-band)"));
  // One role, one value: the three strokes read the same custom property.
  assert.equal(square.declarations.get("stroke"), edge.declarations.get("stroke"));
  assert.equal(open.declarations.get("stroke"), edge.declarations.get("stroke"));
  // Every glyph shape the generator draws is one of the selectors' (a line, a path, a circle).
  const shapes = new Set<string>();
  for (const glyph of golden("ibd-Vehicle.svg").matchAll(/<g class="glyph">(.*?)<\/g>/g)) {
    for (const shape of glyph[1].matchAll(/<([a-z]+) /g)) shapes.add(shape[1]);
  }
  assert.deepEqual([...shapes].sort(), ["circle", "line", "path"]);
});

test("remark 1: a lit or selected port's square and glyph are the selection at 2 px; the lit line keeps 4 px", () => {
  assert.equal(PORT_MARK.length, 1);
  const parts = (mark: string) => [
    `${PALETTE_BODY} svg .port${mark} > rect.port-box:not(.hit-band)`,
    `${PALETTE_BODY} svg .port${mark} .glyph path:not(.hit-band)`,
    `${PALETTE_BODY} svg .port${mark} .glyph line:not(.hit-band)`,
    `${PALETTE_BODY} svg .port${mark} .glyph circle:not(.hit-band)`,
  ];
  assert.deepEqual(PORT_MARK[0].selectors, [...parts(".highlight"), ...parts(".selected")]);
  assert.deepEqual(
    PORT_MARK[0].declarations,
    new Map([
      ["stroke", read("selection")],
      ["stroke-width", "2px"],
    ]),
  );
  assert.equal(PORT_LIT_WIDTH, "2px");
  // No vector-effect anywhere (the 1 px non-scaling variant was rejected: it was thinner
  // than the unselected outline); the lit connector keeps the selection rule's 4 px.
  assert.equal(PORT_MARK[0].declarations.has("vector-effect"), false);
  const selection = markRules(PALETTE_BODY, ".selected");
  assert.equal(selection[0].declarations.get("stroke-width"), MARK_WIDTH);
  assert.ok(selection[0].selectors.includes(`${PALETTE_BODY} svg .highlight path:not(.hit-band)`));
  // It wins over the selection's 4 px: later in the sheet, and more specific (it adds the
  // `.port` class, and `.glyph` or `.port-box`, to the selection rule's selector).
  assert.ok(
    TAB_STYLE.indexOf(PORT_MARK[0].selectors[0]) > TAB_STYLE.indexOf(selection[0].selectors[0]),
  );
  // Only in the palette themes.
  for (const selector of PORT_MARK[0].selectors) assert.ok(selector.startsWith(PALETTE_BODY));
});

test("remark 1: unselected outline < lit port outline < lit connector, as numbers, at any zoom", () => {
  // The unselected widths are the renderer's attributes (no palette rule sets a port's
  // width); every width is in user units and scales with the zoom, so one order holds.
  const svg = golden("ibd-Vehicle.svg");
  const widths = (pattern: RegExp) =>
    new Set([...svg.matchAll(pattern)].map((match) => Number(match[1])));
  const square = widths(/<rect class="port-box"[^>]* stroke-width="([\d.]+)"/g);
  const glyph = widths(/<g class="glyph"><line [^>]*stroke-width="([\d.]+)"/g);
  assert.deepEqual([...square], [1]);
  assert.deepEqual([...glyph], [1.2]);
  const unselected = Math.max(...square, ...glyph);
  const lit = Number.parseFloat(PORT_LIT_WIDTH);
  const connector = Number.parseFloat(MARK_WIDTH);
  assert.ok(unselected < lit && lit < connector, `${unselected} < ${lit} < ${connector}`);
  for (const rule of PALETTE_RULES) {
    if (rule.selectors.some((selector) => /port-box|\.port[.\s]|glyph/.test(selector))) {
      assert.equal(
        rule.declarations.has("stroke-width") && rule !== PORT_MARK[0],
        false,
        rule.selectors.join(", "),
      );
    }
  }
  assert.equal(TAB_STYLE.includes("vector-effect"), false);
});

test("remark 3: a member composition is a compound on every view, its label the title colour", () => {
  const composition = paletteRuleOf(
    ON('[data-role="member"][data-kind="composition"] > rect:not(.hit-band)'),
  );
  assert.deepEqual(composition.selectors, [
    ON('[data-role="member"][data-kind="composition"] > rect:not(.hit-band)'),
  ]);
  assert.deepEqual(
    composition.declarations,
    new Map([
      ["fill", read("compoundFill")],
      ["stroke", read("compoundBorder")],
      ["stroke-width", "3px"],
    ]),
  );
  // No view condition, and after the element rule it overrides in an IBD and an overview
  // (both specificity 0: the later wins).
  assert.equal(composition.selectors[0].includes("data-view"), false);
  assert.ok(
    TAB_STYLE.indexOf(composition.selectors[0]) >
      TAB_STYLE.indexOf(ON('[data-kind="component"] > rect:not(.hit-band)')),
  );
  const label = paletteRuleOf(ON('text.member-label[data-kind="composition"]'));
  assert.deepEqual(label.selectors, [ON('text.member-label[data-kind="composition"]')]);
  assert.deepEqual(label.declarations, new Map([["fill", read("title")]]));
  assert.ok(
    TAB_STYLE.indexOf(label.selectors[0]) >
      TAB_STYLE.indexOf(ON("text.member-label", `${PALETTE_BODY}:not(${PACKED})`)),
  );
  // The generator marks a member composition so in an IBD.
  assert.match(
    golden("ibd-Vehicle.svg"),
    /<g class="member composition" data-role="member"[^>]* data-kind="composition">/,
  );
});

test("remark 4: a port's fill is the enclosing group's colour, the 15 % compound over the canvas", () => {
  for (const scheme of ["light", "dark"] as const) {
    assert.equal(
      likec4Palette(scheme).portFill,
      `color-mix(in srgb, ${LIKEC4_PRIMARY.fill} 15%, ${LIKEC4_CANVAS[scheme]})`,
    );
  }
  assert.equal(portFill("#3b82f6", "#fff"), "color-mix(in srgb, #3b82f6 15%, #fff)");
  const square = paletteRuleOf(ON(".port-box:not(.hit-band)"));
  assert.equal(square.declarations.get("fill"), read("portFill"));
  // An open glyph path (fill="none") is never filled.
  assert.equal(
    paletteRuleOf(ON('g.port g.glyph path[fill="none"]:not(.hit-band)')).declarations.has("fill"),
    false,
  );
});

test("none of the four remarks reaches a high-contrast body", () => {
  const touching = TAB_RULES.filter((rule) =>
    rule.selectors.some((selector) =>
      /port-box|\.glyph|\[data-kind="composition"\]/.test(selector),
    ),
  );
  assert.ok(touching.length >= 5);
  for (const rule of touching) {
    for (const selector of rule.selectors) {
      assert.ok(
        selector.startsWith(`:where(${PALETTE_BODY}`) || selector.startsWith(PALETTE_BODY),
        selector,
      );
    }
  }
});

// --- the page's own blocks ----------------------------------------------------------------

test("parseModelBlock reads a golden page's block; the allowlist holds its {file, member} pairs", () => {
  const block = parseModelBlock(golden("ibd-Chassis.html"));
  assert.equal(block.kind, "ok");
  const entries = block.kind === "ok" ? block.value : new Map();
  assert.deepEqual(entries.get("11"), { file: "swc/chassis/Sensor.yaml", member: "ports/pSpeed" });
  assert.deepEqual(entries.get("0"), { file: "compositions/Chassis.yaml", member: null });
  const allowed = modelAllowlist(block);
  assert.ok(allowed.has("swc/chassis/Sensor.yaml\u0000\u0002ports/pSpeed"));
  assert.ok(allowed.has("compositions/Chassis.yaml\u0000\u0001"), "a null member's own marker");
});

function withModelBlock(text: string): string {
  return `<html><head></head><body><script type="application/json" id="swcraft-model">${text}</script></body></html>`;
}

test("parseModelBlock: an absent block, malformed JSON and every wrong shape open nothing", () => {
  assert.deepEqual(parseModelBlock("<html><head></head><body></body></html>"), { kind: "absent" });
  assert.equal(parseModelBlock(golden("index.html")).kind, "absent");
  for (const text of [
    "{",
    "[]",
    "null",
    '{"x": {"file": "a.yaml", "member": null}}',
    '{"01": {"file": "a.yaml", "member": null}}',
    '{"0": {"file": "a.yaml"}}',
    '{"0": {"file": "a.yaml", "member": 1}}',
    '{"0": {"file": "a.yaml", "member": null, "extra": 1}}',
    '{"0": {"refused": "maybe"}}',
    '{"0": []}',
  ]) {
    const block = parseModelBlock(withModelBlock(text));
    assert.equal(block.kind, "malformed", text);
    assert.equal(modelAllowlist(block).size, 0, text);
  }
  const refused = parseModelBlock(
    withModelBlock('{"0": {"refused": "several-files"}, "1": {"refused": "no-file"}}'),
  );
  assert.equal(refused.kind, "ok");
  assert.equal(modelAllowlist(refused).size, 0, "a refusal opens nothing");
  const twice = withModelBlock("{}").replace(
    "</body>",
    '<script type="application/json" id="swcraft-model">{}</script></body>',
  );
  assert.equal(parseModelBlock(twice).kind, "malformed");
});

test("parseElementsBlock reads the golden index; viewsForElement answers in the index's order", () => {
  const block = parseElementsBlock(golden("index.html"));
  assert.equal(block.kind, "ok");
  const elements = block.kind === "ok" ? block.value : new Map();
  assert.deepEqual(viewsForElement(elements, "compositions/Chassis.yaml", null), [
    "ibd-Chassis",
    "overview-Chassis",
    "ibd-Vehicle",
    "overview-Vehicle",
    "nesting",
  ]);
  assert.deepEqual(viewsForElement(elements, "compositions/Chassis.yaml", "ports/pSpeedOut"), [
    "ibd-Chassis",
    "ibd-Vehicle",
  ]);
  assert.deepEqual(
    viewsForElement(elements, "compositions/Chassis.yaml", "ports/notDrawn"),
    viewsForElement(elements, "compositions/Chassis.yaml", null),
    "a member no view draws falls back to the file",
  );
  assert.deepEqual(viewsForElement(elements, "swc/none/Nothing.yaml", null), []);
  assert.deepEqual(
    viewsForElement(elements, "compositions/Chassis.yaml", "constructor"),
    viewsForElement(elements, "compositions/Chassis.yaml", null),
  );
});

test("parseElementsBlock: an absent block, malformed JSON and a wrong shape", () => {
  assert.equal(parseElementsBlock(golden("ibd-Chassis.html")).kind, "absent");
  const of = (text: string) =>
    parseElementsBlock(
      `<body><script type="application/json" id="swcraft-elements">${text}</script></body>`,
    );
  for (const text of [
    "{",
    "[]",
    '{"a.yaml": []}',
    '{"a.yaml": {"views": "ibd-Root", "members": {}}}',
    '{"a.yaml": {"views": [], "members": []}}',
    '{"a.yaml": {"views": [], "members": {"ports/p": [1]}}}',
    '{"a.yaml": {"views": []}}',
  ]) {
    assert.equal(of(text).kind, "malformed", text);
  }
  assert.equal(of('{"a.yaml": {"views": ["ibd-Root"], "members": {}}}').kind, "ok");
});

test("the pick lists each view as the index names it, in the order given", () => {
  const headings = indexHeadings(golden("index.html"));
  assert.equal(headings.get("ibd-Chassis"), "Chassis — internal block diagram");
  assert.equal(headings.get("deployment")?.endsWith("deployment"), true);
  assert.deepEqual(pickItems(["nesting", "ibd-Chassis", "gone"], headings), [
    { view: "nesting", label: headings.get("nesting") },
    { view: "ibd-Chassis", label: "Chassis — internal block diagram" },
    { view: "gone", label: "gone" },
  ]);
  const escaped = indexHeadings('<li><a href="ibd-%C3%9Cber.html">A &amp; B &lt;C&gt;</a></li>');
  assert.equal(escaped.get("ibd-\u00dcber"), "A & B <C>");
});

// --- what crosses the boundary ---------------------------------------------------------

const LISTING = [
  "index.html",
  "report.txt",
  "ibd-Root.html",
  "ibd-Root.svg",
  "ibd-\u00dcber.html",
  "sequence-a b.html",
];

test("acceptNavigate follows the listed pages, the index, report.txt and an .svg, decoding once", () => {
  assert.deepEqual(acceptNavigate("index.html", LISTING), {
    kind: "page",
    view: undefined,
    fragment: "",
  });
  assert.deepEqual(acceptNavigate("ibd-Root.html", LISTING), {
    kind: "page",
    view: "ibd-Root",
    fragment: "",
  });
  assert.deepEqual(acceptNavigate("ibd-Root.html#Chassis", LISTING), {
    kind: "page",
    view: "ibd-Root",
    fragment: "Chassis",
  });
  assert.deepEqual(acceptNavigate("ibd-Root.html#a%20b%23c", LISTING), {
    kind: "page",
    view: "ibd-Root",
    fragment: "a b#c",
  });
  assert.deepEqual(acceptNavigate("ibd-%C3%9Cber.html", LISTING), {
    kind: "page",
    view: "ibd-\u00dcber",
    fragment: "",
  });
  assert.deepEqual(acceptNavigate("sequence-a%20b.html", LISTING), {
    kind: "page",
    view: "sequence-a b",
    fragment: "",
  });
  assert.deepEqual(acceptNavigate("ibd-Root.svg", LISTING), { kind: "file", name: "ibd-Root.svg" });
  assert.deepEqual(acceptNavigate("report.txt", LISTING), { kind: "file", name: "report.txt" });
});

test("acceptNavigate refuses every other form", () => {
  for (const href of [
    "sub/ibd-Root.html",
    "%2Fibd-Root.html",
    "..%2Fibd-Root.html",
    "../ibd-Root.html",
    "..",
    "%2E%2E%2Fibd-Root.html",
    "ibd%252DRoot.html",
    "ibd-Root.html%23x",
    "..\\ibd-Root.html",
    "file:///etc/ibd-Root.html",
    "https://example.com/ibd-Root.html",
    "javascript:alert(1)",
    "other.html",
    "IBD-Root.html",
    "ibd-root.html",
    "ibd-Root.svg#x",
    "report.txt#x",
    "ibd-Root.css",
    "%E0%A4%A.html",
    "ibd-Root.html\u0000",
    ".html",
    "",
  ]) {
    assert.equal(acceptNavigate(href, LISTING).kind, "refused", href);
  }
  assert.equal(acceptNavigate("ibd-Root.html", undefined).kind, "refused", "no listing, no link");
});

/** A scratch directory the test removes when it ends (links and the FIFO with it). */
function scratch(t: TestContext): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-diagrams-tab-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

function tryLink(target: string, link: string, kind: "file" | "dir"): boolean {
  try {
    fs.symlinkSync(target, link, kind);
    return true;
  } catch {
    return false;
  }
}

test("resolveViewsFile reads only a regular file whose real path lies inside views_root", (t) => {
  const root = scratch(t);
  const views = path.join(root, "views");
  fs.mkdirSync(views);
  fs.writeFileSync(path.join(views, "ibd-Root.html"), "<html></html>", { encoding: "utf8" });
  fs.writeFileSync(path.join(root, "outside.html"), "secret", { encoding: "utf8" });
  fs.mkdirSync(path.join(views, "dir.html"));
  const real = fs.realpathSync.native(path.join(views, "ibd-Root.html"));
  assert.deepEqual(resolveViewsFile(views, "ibd-Root.html"), { kind: "file", path: real });
  assert.equal(resolveViewsFile(views, "dir.html").kind, "refused", "a directory");
  assert.equal(resolveViewsFile(views, "missing.html").kind, "refused");
  for (const name of ["../outside.html", "sub/x.html", "..", ".", "", "a\\b.html", "C:x.html"]) {
    assert.equal(resolveViewsFile(views, name).kind, "refused", name);
  }
  if (tryLink(path.join(root, "outside.html"), path.join(views, "linked.html"), "file")) {
    assert.equal(
      resolveViewsFile(views, "linked.html").kind,
      "refused",
      "a listed link to a file outside",
    );
    assert.ok(tryLink(path.join(views, "ibd-Root.html"), path.join(views, "inner.html"), "file"));
    assert.deepEqual(
      resolveViewsFile(views, "inner.html"),
      { kind: "file", path: real },
      "a link inside is read",
    );
  } else {
    t.diagnostic("symbolic links cannot be created here; the link cases are not run");
  }
  if (process.platform !== "win32") {
    execFileSync("mkfifo", [path.join(views, "fifo.html")]);
    assert.equal(resolveViewsFile(views, "fifo.html").kind, "refused", "a FIFO");
  }
});

test("followLink: a listed link that is no regular file inside views_root is refused like an unlisted one", (t) => {
  // It must not reach the missing-page refusal.
  const root = scratch(t);
  const views = path.join(root, "views");
  fs.mkdirSync(views);
  fs.writeFileSync(path.join(views, "index.html"), "<html></html>", { encoding: "utf8" });
  fs.writeFileSync(path.join(views, "ibd-Root.html"), "<html></html>", { encoding: "utf8" });
  fs.writeFileSync(path.join(views, "ibd-Root.svg"), "<svg/>", { encoding: "utf8" });
  fs.writeFileSync(path.join(root, "outside.html"), "secret", { encoding: "utf8" });
  fs.mkdirSync(path.join(views, "dir.html"));
  const listing = () => fs.readdirSync(views);
  assert.deepEqual(followLink("ibd-Root.html#Chassis", views, listing()), {
    kind: "page",
    view: "ibd-Root",
    fragment: "Chassis",
  });
  assert.deepEqual(followLink("index.html", views, listing()), {
    kind: "page",
    view: undefined,
    fragment: "",
  });
  assert.deepEqual(followLink("ibd-Root.svg", views, listing()), {
    kind: "file",
    name: "ibd-Root.svg",
    path: fs.realpathSync.native(path.join(views, "ibd-Root.svg")),
  });
  assert.equal(followLink("unlisted.html", views, listing()).kind, "refused", "unlisted");
  assert.equal(followLink("dir.html", views, listing()).kind, "refused", "a listed directory");
  assert.equal(followLink("ibd-Root.html", views, ["ibd-Root.html", "gone.html"]).kind, "page");
  assert.deepEqual(
    followLink("gone.html", views, ["ibd-Root.html", "gone.html"]),
    { kind: "refused", reason: "the file does not exist" },
    "listed, then removed before the click",
  );
  if (tryLink(path.join(root, "outside.html"), path.join(views, "linked.html"), "file")) {
    assert.deepEqual(followLink("linked.html", views, listing()), {
      kind: "refused",
      reason: "the file lies outside the diagrams' directory",
    });
    assert.ok(tryLink(path.join(root, "outside.html"), path.join(views, "linked.svg"), "file"));
    assert.equal(followLink("linked.svg", views, listing()).kind, "refused", "an .svg likewise");
  } else {
    t.diagnostic("symbolic links cannot be created here; the link cases are not run");
  }
});

test("checkStorageName: a storage name's lexical rule", () => {
  for (const name of [
    "swc/chassis/Sensor.yaml",
    "compositions/Root.yaml",
    "swc/g/Name~2.yaml",
    "a.yaml",
  ]) {
    assert.equal(checkStorageName(name), undefined, name);
  }
  for (const name of [
    "",
    "/etc/x.yaml",
    "C:/x.yaml",
    "C:x.yaml",
    "//server/share/x.yaml",
    "\\\\server\\share\\x.yaml",
    "swc\\a.yaml",
    "../x.yaml",
    "a/../x.yaml",
    "./a.yaml",
    "a/./b.yaml",
    "a//b.yaml",
    "a/",
    "a\u0000.yaml",
    "a\nb.yaml",
    "a.yml",
    "a.txt",
    "file:a.yaml",
    `${"x".repeat(1100)}.yaml`,
  ]) {
    assert.notEqual(checkStorageName(name), undefined, JSON.stringify(name));
  }
});

test("acceptOpen opens only the page's own {file, member} pairs, resolved under the real model_root", (t) => {
  const root = scratch(t);
  const model = path.join(root, "model");
  fs.mkdirSync(path.join(model, "swc", "a"), { recursive: true });
  fs.writeFileSync(
    path.join(model, "swc", "a", "Sensor.yaml"),
    "kind: ApplicationSwComponentType\n",
    { encoding: "utf8" },
  );
  fs.mkdirSync(path.join(model, "dir.yaml"));
  fs.writeFileSync(path.join(root, "outside.yaml"), "x: 1\n", { encoding: "utf8" });
  const links = tryLink(
    path.join(root, "outside.yaml"),
    path.join(model, "swc", "a", "link.yaml"),
    "file",
  );
  const entries = [
    ["swc/a/Sensor.yaml", "ports/p"],
    ["swc/a/Sensor.yaml", null],
    ["dir.yaml", null],
    ["swc/a/link.yaml", null],
    ["swc/a/missing.yaml", null],
    ["/etc/passwd.yaml", null],
    ["C:/x.yaml", null],
    ["//server/share/x.yaml", null],
    ["swc\\a\\Sensor.yaml", null],
    ["../outside.yaml", null],
    ["swc//a/Sensor.yaml", null],
    ["swc/a/Sensor.yaml\u0000", null],
    ["swc/a/Sensor.txt", null],
  ] as const;
  const block = parseModelBlock(
    withModelBlock(
      JSON.stringify(
        Object.fromEntries(entries.map(([file, member], i) => [String(i), { file, member }])),
      ),
    ),
  );
  assert.equal(block.kind, "ok");
  const allowed = modelAllowlist(block);
  const real = fs.realpathSync.native(path.join(model, "swc", "a", "Sensor.yaml"));
  assert.deepEqual(acceptOpen("swc/a/Sensor.yaml", "ports/p", allowed, model), {
    kind: "open",
    path: real,
    member: "ports/p",
  });
  assert.deepEqual(acceptOpen("swc/a/Sensor.yaml", null, allowed, model), {
    kind: "open",
    path: real,
    member: null,
  });
  assert.equal(
    acceptOpen("swc/a/Sensor.yaml", "ports/q", allowed, model).kind,
    "refused",
    "a pair the page does not list",
  );
  assert.equal(acceptOpen("swc/a/Other.yaml", null, allowed, model).kind, "refused");
  const reasons: Record<string, string> = {
    "dir.yaml": "the name is not a regular file",
    "swc/a/link.yaml": "the file lies outside the model",
    "swc/a/missing.yaml": "the file does not exist",
    "/etc/passwd.yaml": "the name is absolute",
    "C:/x.yaml": "the name holds a backslash, a colon or a control character",
    "//server/share/x.yaml": "the name is absolute",
    "swc\\a\\Sensor.yaml": "the name holds a backslash, a colon or a control character",
    "../outside.yaml": "the name has an empty, '.' or '..' segment",
    "swc//a/Sensor.yaml": "the name has an empty, '.' or '..' segment",
    "swc/a/Sensor.yaml\u0000": "the name holds a backslash, a colon or a control character",
    "swc/a/Sensor.txt": "the name is not a .yaml file",
  };
  for (const [file, member] of entries.slice(2)) {
    if (file === "swc/a/link.yaml" && !links) {
      t.diagnostic("symbolic links cannot be created here; the link case is not run");
      continue;
    }
    assert.deepEqual(
      acceptOpen(file, member, allowed, model),
      { kind: "refused", reason: reasons[file] },
      JSON.stringify(file),
    );
  }
  // A null member and an empty one are two pairs: the page's {file, null} allows no ""-member open.
  assert.equal(acceptOpen("swc/a/Sensor.yaml", "", allowed, model).kind, "refused");
  const emptyMember = modelAllowlist(
    parseModelBlock(
      withModelBlock(JSON.stringify({ "0": { file: "swc/a/Sensor.yaml", member: "" } })),
    ),
  );
  assert.equal(acceptOpen("swc/a/Sensor.yaml", null, emptyMember, model).kind, "refused");
  assert.equal(acceptOpen("swc/a/Sensor.yaml", "", emptyMember, model).kind, "open");
  assert.equal(
    acceptOpen("swc/a/Sensor.yaml", null, new Set(), model).kind,
    "refused",
    "an empty allowlist opens nothing",
  );
});

test("resolveModelFile: a storage name passed lexically still has to be a regular file under the real model", (t) => {
  const root = scratch(t);
  const model = path.join(root, "model");
  fs.mkdirSync(path.join(model, "swc"), { recursive: true });
  fs.writeFileSync(path.join(model, "swc", "A.yaml"), "a: 1\n", { encoding: "utf8" });
  assert.equal(resolveModelFile(model, "swc/A.yaml").kind, "file");
  assert.equal(resolveModelFile(model, "swc/B.yaml").kind, "refused");
  assert.equal(resolveModelFile(path.join(root, "nowhere"), "swc/A.yaml").kind, "refused");
});

test("parseTabMessage takes the six messages of the tab's protocol and nothing else", () => {
  assert.deepEqual(parseTabMessage({ type: "navigate", href: "a.html" }), {
    type: "navigate",
    href: "a.html",
  });
  assert.deepEqual(parseTabMessage({ type: "open", file: "a.yaml", member: null }), {
    type: "open",
    file: "a.yaml",
    member: null,
  });
  assert.deepEqual(parseTabMessage({ type: "open", file: "a.yaml", member: "ports/p", extra: 1 }), {
    type: "open",
    file: "a.yaml",
    member: "ports/p",
  });
  for (const type of ["reload", "back", "forward", "generate"]) {
    assert.deepEqual(parseTabMessage({ type, junk: "x" }), { type });
  }
  for (const raw of [
    null,
    "navigate",
    {},
    { type: "navigate" },
    { type: "navigate", href: 1 },
    { type: "navigate", href: "x".repeat(5000) },
    { type: "open", file: "a.yaml" },
    { type: "open", file: "a.yaml", member: 3 },
    { type: "loaded" },
    { type: "eval", code: "x" },
  ]) {
    assert.equal(parseTabMessage(raw), undefined, JSON.stringify(raw)?.slice(0, 60));
  }
});

// --- the order of a show and the history ---------------------------------------------------

test("planShow: a reveal on the page already shown reveals the panel first, then posts", () => {
  const reveal = { file: "swc/a/S.yaml", member: null, name: "S" };
  assert.deepEqual(
    planShow(
      { entry: { view: "ibd-Root", fragment: "" }, page: true },
      { entry: { view: "ibd-Root", fragment: "" }, reveal },
    ),
    [{ kind: "reveal-panel" }, { kind: "post-reveal", reveal }],
  );
});

test("planShow: a reveal over a refusal shown in the view's place renders — no page script would take the post", () => {
  const reveal = { file: "swc/a/S.yaml", member: null, name: "S" };
  const entry = { view: "ibd-Root", fragment: "" };
  assert.deepEqual(planShow({ entry, page: false }, { entry, reveal }), [
    { kind: "render", entry, reveal },
    { kind: "reveal-panel" },
  ]);
});

test("planShow: anything else renders the page with the reveal in its state, then reveals the panel", () => {
  const reveal = { file: "swc/a/S.yaml", member: null, name: "S" };
  const entry = { view: "ibd-Root", fragment: "" };
  assert.deepEqual(planShow(undefined, { entry, reveal }), [
    { kind: "render", entry, reveal },
    { kind: "reveal-panel" },
  ]);
  assert.deepEqual(
    planShow({ entry: { view: "nesting", fragment: "" }, page: true }, { entry, reveal }),
    [{ kind: "render", entry, reveal }, { kind: "reveal-panel" }],
  );
  assert.deepEqual(
    planShow({ entry: { view: "ibd-Root", fragment: "" }, page: true }, { entry }),
    [{ kind: "render", entry, reveal: null }, { kind: "reveal-panel" }],
    "a plain show of the same page re-reads it",
  );
  const withFragment = { view: "ibd-Root", fragment: "Chassis" };
  assert.deepEqual(
    planShow(
      { entry: { view: "ibd-Root", fragment: "" }, page: true },
      { entry: withFragment, reveal },
    ),
    [{ kind: "render", entry: withFragment, reveal }, { kind: "reveal-panel" }],
  );
});

test("TabHistory: visits, Back and Forward, a new visit dropping what lay ahead", () => {
  const history = new TabHistory();
  assert.equal(history.current, undefined);
  assert.equal(history.back(), undefined);
  history.visit({ view: undefined, fragment: "" });
  history.visit({ view: "ibd-Vehicle", fragment: "" });
  history.visit({ view: "ibd-Chassis", fragment: "" });
  assert.deepEqual([history.canBack, history.canForward], [true, false]);
  assert.deepEqual(history.back(), { view: "ibd-Vehicle", fragment: "" });
  assert.deepEqual(history.back(), { view: undefined, fragment: "" });
  assert.equal(history.back(), undefined);
  assert.deepEqual(history.forward(), { view: "ibd-Vehicle", fragment: "" });
  history.visit({ view: "ibd-Vehicle", fragment: "" });
  assert.equal(history.canForward, true, "a visit of the current place changes nothing");
  history.visit({ view: "nesting", fragment: "" });
  assert.equal(history.canForward, false, "a new visit drops what lay ahead");
  assert.deepEqual(history.back(), { view: "ibd-Vehicle", fragment: "" });
  history.visit({ view: "ibd-Vehicle", fragment: "Chassis" });
  assert.deepEqual(
    history.current,
    { view: "ibd-Vehicle", fragment: "Chassis" },
    "another fragment is another place",
  );
  history.clear();
  assert.equal(history.current, undefined);
});

test("TabHistory keeps the last HISTORY_LIMIT places", () => {
  const history = new TabHistory();
  for (let i = 0; i < HISTORY_LIMIT + 5; i += 1) {
    history.visit({ view: `v${i}`, fragment: "" });
  }
  let steps = 0;
  while (history.back() !== undefined) {
    steps += 1;
  }
  assert.equal(steps, HISTORY_LIMIT - 1);
  assert.deepEqual(history.current, { view: "v5", fragment: "" });
});

// --- the panel's small decisions ---------------------------------------------------------

test("TabState: a visit under another views_root forgets the old root's history first", () => {
  const state = new TabState();
  state.visit("/a/views", { view: "ibd-Root", fragment: "" });
  state.visit("/a/views", { view: "nesting", fragment: "" });
  assert.equal(state.history.canBack, true);
  state.visit("/b/views", { view: undefined, fragment: "" });
  assert.equal(state.viewsRoot, "/b/views");
  assert.equal(state.history.canBack, false, "Back never renders /a's view under /b");
  assert.equal(state.history.back(), undefined);
  assert.equal(state.shownIn("/a/views"), undefined, "nothing is shown from the old root");
  assert.deepEqual(state.shownIn("/b/views"), {
    entry: { view: undefined, fragment: "" },
    page: false,
  });
  state.visit("/b/views", { view: "ibd-Root", fragment: "" });
  assert.equal(state.history.canBack, true, "the same root keeps its history");
});

test("TabState: a page shown sets its allowlist; a refusal clears it and is not a page", () => {
  const state = new TabState();
  const entry = { view: "ibd-Root", fragment: "" };
  state.visit("/w/views", entry);
  const allowlist = modelAllowlist(parseModelBlock(golden("ibd-Chassis.html")));
  assert.ok(allowlist.size > 0);
  state.showedPage(allowlist, false);
  assert.equal(state.allowlist, allowlist);
  assert.deepEqual(state.shownIn("/w/views"), { entry, page: true });
  assert.equal(state.shownViewIn("/w/views"), "ibd-Root");
  assert.equal(state.shownViewIn("/other/views"), undefined, "another root shows nothing");
  assert.equal(state.shownOutOfDate, false);
  state.showedRefusal();
  assert.equal(state.allowlist.size, 0, "a refusal opens nothing");
  assert.deepEqual(state.shownIn("/w/views"), { entry, page: false });
  assert.equal(state.shownOutOfDate, undefined, "a refusal is told nothing");
  const reveal = { file: "swc/a/S.yaml", member: null, name: "S" };
  assert.equal(
    planShow(state.shownIn("/w/views"), { entry, reveal })[0].kind,
    "render",
    "a reveal over the refusal renders the page again",
  );
});

test("TabState: the out-of-date reading is told only to a page, and only when it moved", () => {
  const state = new TabState();
  assert.equal(state.outOfDateMoved(true), false, "no page shown");
  state.visit("/w/views", { view: undefined, fragment: "" });
  state.showedPage(new Set(), false);
  assert.equal(state.outOfDateMoved(false), false);
  assert.equal(state.outOfDateMoved(true), true);
  assert.equal(state.shownOutOfDate, true);
  assert.equal(state.outOfDateMoved(true), false);
  state.showedRefusal();
  assert.equal(state.outOfDateMoved(false), false);
});

test("TabState: dispose forgets the places, the root, the allowlist and the editor column", () => {
  const state = new TabState();
  state.visit("/w/views", { view: "ibd-Root", fragment: "" });
  state.showedPage(modelAllowlist(parseModelBlock(golden("ibd-Chassis.html"))), true);
  state.editorColumn = 2;
  state.disposed();
  assert.equal(state.history.current, undefined);
  assert.equal(state.viewsRoot, undefined);
  assert.equal(state.allowlist.size, 0);
  assert.equal(state.editorColumn, undefined);
  assert.equal(state.shownOutOfDate, undefined);
  assert.equal(state.shownIn("/w/views"), undefined);
});

test("besideColumn: the remembered column while it is not the panel's own, otherwise beside", () => {
  assert.equal(besideColumn(undefined, 1), "beside", "the first open goes beside");
  assert.equal(besideColumn(2, 1), 2, "then the column the file took");
  // The panel dragged into the remembered group must not be covered.
  assert.equal(besideColumn(2, 2), "beside", "never over the panel");
  assert.equal(besideColumn(1, 2), 1, "the panel moved elsewhere: the files' column stays");
  assert.equal(besideColumn(2, undefined), 2, "no reported panel column decides nothing");
  assert.equal(besideColumn(undefined, undefined), "beside");
});

test("TabState: the panel moved into the remembered column forgets it; elsewhere keeps it", () => {
  const state = new TabState();
  state.editorColumn = 2;
  state.panelMovedTo(3);
  assert.equal(state.editorColumn, 2);
  state.panelMovedTo(undefined);
  assert.equal(state.editorColumn, 2);
  state.panelMovedTo(2);
  assert.equal(state.editorColumn, undefined);
  assert.equal(besideColumn(state.editorColumn, 2), "beside");
});

test("refusalLine: a page the press just generated and still another release's names the release mismatch", () => {
  const otherRelease = { reason: "other-release" as const, line: OTHER_RELEASE_LINE };
  assert.equal(refusalLine(otherRelease, false, "0.23.0"), OTHER_RELEASE_LINE);
  const line = refusalLine(otherRelease, true, "0.23.0");
  assert.equal(line, generatedByAnotherReleaseLine("0.23.0"));
  assert.notEqual(line, OTHER_RELEASE_LINE);
  assert.match(line, /another release than this extension \(0\.23\.0\)/);
  assert.match(line, /swcraft\.executable or PATH/);
  assert.equal(/[\\/]Users|[A-Za-z]:\\/.test(line), false, "no path");
  for (const reason of ["no-head", "foreign-meta", "foreign-tag", "several-scripts"] as const) {
    assert.equal(
      refusalLine({ reason, line: UNREADABLE_PAGE_LINE }, true, "0.23.0"),
      UNREADABLE_PAGE_LINE,
      reason,
    );
  }
});

test("the panel's view type is swcraft.diagrams", () => {
  assert.equal(DIAGRAMS_TAB_VIEW_TYPE, "swcraft.diagrams");
});

test("tabTitle: a view page's heading, `Diagrams — <project>` for the index, `Diagrams` without one", () => {
  assert.equal(
    tabTitle(golden("ibd-Chassis.html"), "ibd-Chassis"),
    "Chassis — internal block diagram",
  );
  const index = golden("index.html");
  const project = /<h1>([^<]*)<\/h1>/.exec(index)?.[1] ?? "";
  assert.notEqual(project, "");
  assert.equal(tabTitle(index, undefined), `${DIAGRAMS_TAB_TITLE} — ${project}`);
  assert.equal(tabTitle("<h1>A &amp; B &lt;x&gt;</h1>", "v"), "A & B <x>");
  assert.equal(tabTitle("<html><body>no heading</body></html>", "v"), DIAGRAMS_TAB_TITLE);
  assert.equal(tabTitle("<h1>  </h1>", undefined), DIAGRAMS_TAB_TITLE);
});

/** One outline symbol, its selection on *line*. */
function symbol(
  name: string,
  line: number,
  children: OutlineSymbol[] = [],
  rangeStart = line,
): OutlineSymbol {
  const span = (startLine: number) => ({
    startLine,
    startCharacter: 0,
    endLine: startLine,
    endCharacter: 1,
  });
  return {
    name,
    detail: "",
    kind: "Object",
    range: span(rangeStart),
    selection: span(line),
    children,
  };
}

test("memberLineOf: the member through its group, else the identity line", () => {
  const outline = [
    symbol(
      "Sensor",
      1,
      [
        symbol("ports", 3, [symbol("pSpeed", 4), symbol("pTemp", 7)]),
        symbol("runnables", 10, [symbol("SensorRun", 11)]),
      ],
      0,
    ),
  ];
  assert.equal(memberLineOf(outline, "ports/pTemp"), 7);
  assert.equal(memberLineOf(outline, "runnables/SensorRun"), 11);
  assert.equal(memberLineOf(outline, null), 1, "no member: the identity line");
  assert.equal(memberLineOf(outline, "ports/gone"), 1, "a member not found: the identity line");
  assert.equal(memberLineOf(outline, "delegations/pSpeed"), 1, "a group not found");
  assert.equal(memberLineOf(outline, "pSpeed"), 1, "not <group>/<name>");
  assert.equal(memberLineOf(outline, "ports/"), 1);
  assert.equal(memberLineOf(outline, "/pSpeed"), 1);
  assert.equal(memberLineOf([], "ports/pSpeed"), 0, "no outline: line 0");
  // No identity line read: the groups are the top level, and the fallback is line 0.
  const rootless = [symbol("ports", 2, [symbol("pSpeed", 3)]), symbol("runnables", 6)];
  assert.equal(memberLineOf(rootless, "ports/pSpeed"), 3);
  assert.equal(memberLineOf(rootless, null), 0);
  assert.equal(memberLineOf([symbol("ports", 2, [symbol("pSpeed", 3)])], null), 0);
});

test("memberLineOf over a real outline: the fixture's Sensor, pSpeed at its `- name:` line", () => {
  const modelFile = path.join(
    PROJECT_ROOT,
    "..",
    "packages",
    "tools",
    "tests",
    "fixtures",
    "diagrams",
    "model",
    "swc",
    "chassis",
    "Sensor.yaml",
  );
  const text = fs.readFileSync(modelFile, "utf8");
  const refs = loadReferences(fs.readFileSync(path.join(SCHEMAS_DIR, "references.json"), "utf8"));
  const outline = outlineOf(refs, text, "swc");
  const lines = text.split(/\r?\n/);
  assert.match(lines[memberLineOf(outline, "ports/pSpeed")], /- name: pSpeed\b/);
  assert.match(lines[memberLineOf(outline, null)], /^name: Sensor\b/);
});

test("generateArgument: the tab's Generate asks for the view shown, nothing for the index", () => {
  assert.deepEqual(generateArgument({ view: "ibd-Chassis", fragment: "Sensor" }), {
    view: "ibd-Chassis",
  });
  assert.equal(generateArgument({ view: undefined, fragment: "" }), undefined);
  assert.equal(generateArgument(undefined), undefined);
});

// --- Show in Diagram --------------------------------------------------------------------

test("readDiagramIndex: the golden index's elements block and labels, the pick in the index's order", () => {
  const index = readDiagramIndex(golden("index.html"));
  assert.equal(index.elements.kind, "ok");
  const elements = index.elements.kind === "ok" ? index.elements.value : new Map();
  const views = viewsForElement(elements, "swc/chassis/Sensor.yaml", "ports/pSpeed");
  assert.ok(views.includes("ibd-Chassis"), JSON.stringify(views));
  const sensor = viewsForElement(elements, "swc/chassis/Sensor.yaml", null);
  assert.ok(sensor.length > 1, "the fixture draws Sensor on several views");
  const choice = chooseView(sensor, undefined);
  assert.equal(choice.kind, "pick");
  const labels = pickItems(choice.kind === "pick" ? choice.views : [], index.headings);
  assert.deepEqual(
    labels.map((item) => item.view),
    sensor,
    "the pick keeps the index's order",
  );
  assert.equal(
    labels.find((item) => item.view === "ibd-Chassis")?.label,
    "Chassis — internal block diagram",
  );
  assert.equal(readDiagramIndex("<html><body></body></html>").elements.kind, "absent");
});

test("lexicalStorageName: the document's path under model_root, POSIX; nothing outside it", () => {
  const root = path.join(os.tmpdir(), "project", "model");
  assert.equal(
    lexicalStorageName(root, path.join(root, "swc", "chassis", "Sensor.yaml")),
    "swc/chassis/Sensor.yaml",
  );
  assert.equal(lexicalStorageName(root, path.join(root, "tasks", "T~1.yaml")), "tasks/T~1.yaml");
  assert.equal(lexicalStorageName(root, root), undefined);
  assert.equal(lexicalStorageName(root, path.join(root, "..", "other", "A.yaml")), undefined);
  assert.equal(lexicalStorageName(root, path.join(os.tmpdir(), "elsewhere.yaml")), undefined);
});

test("indexedStorageName: lexically first, then by real path; two names on one file are ambiguous", (t) => {
  const root = scratch(t);
  const model = path.join(root, "model");
  fs.mkdirSync(path.join(model, "swc", "chassis"), { recursive: true });
  fs.writeFileSync(path.join(model, "swc", "chassis", "Sensor.yaml"), "name: Sensor\n", {
    encoding: "utf8",
  });
  fs.writeFileSync(path.join(model, "swc", "chassis", "Other.yaml"), "name: Other\n", {
    encoding: "utf8",
  });
  const views = { views: ["ibd-Chassis"], members: {} };
  const elements = new Map([["swc/chassis/Sensor.yaml", views]]);
  const real = (name: string) => fs.realpathSync.native(path.join(model, ...name.split("/")));
  let asked = 0;
  const byReal = () => {
    asked += 1;
    return realPathIndex(model, elements.keys());
  };
  // Lexically listed: the real paths are never computed.
  assert.equal(
    indexedStorageName(
      elements,
      "swc/chassis/Sensor.yaml",
      real("swc/chassis/Sensor.yaml"),
      byReal,
    ),
    "swc/chassis/Sensor.yaml",
  );
  assert.equal(asked, 0, "the lexical name answers without a realpath");
  // A file the index does not list, by either spelling.
  assert.equal(
    indexedStorageName(elements, "swc/chassis/Other.yaml", real("swc/chassis/Other.yaml"), byReal),
    undefined,
  );
  assert.equal(indexedStorageName(elements, undefined, undefined, byReal), undefined);
  // An internal link: the document opened through a link inside the model, the index
  // listing the file's own name — found by its real path.
  const link = path.join(model, "swc", "Linked.yaml");
  if (!tryLink(path.join(model, "swc", "chassis", "Sensor.yaml"), link, "file")) {
    t.skip("this file system makes no symbolic link");
    return;
  }
  assert.equal(
    indexedStorageName(
      elements,
      lexicalStorageName(model, link),
      fs.realpathSync.native(link),
      byReal,
    ),
    "swc/chassis/Sensor.yaml",
  );
  // The other way round: the index lists the link's name (the generator lists without
  // resolving links), the document is the file itself.
  const viaLink = new Map([["swc/Linked.yaml", views]]);
  assert.equal(
    indexedStorageName(viaLink, "swc/chassis/Sensor.yaml", real("swc/chassis/Sensor.yaml"), () =>
      realPathIndex(model, viaLink.keys()),
    ),
    "swc/Linked.yaml",
  );
  // Two indexed names resolving to one file: ambiguous, not on any diagram.
  const both = new Map([
    ["swc/chassis/Sensor.yaml", views],
    ["swc/Linked.yaml", views],
  ]);
  const twice = realPathIndex(model, both.keys());
  assert.deepEqual(twice.get(real("swc/chassis/Sensor.yaml")), [
    "swc/chassis/Sensor.yaml",
    "swc/Linked.yaml",
  ]);
  assert.equal(
    indexedStorageName(both, "swc/Elsewhere.yaml", real("swc/chassis/Sensor.yaml"), () => twice),
    undefined,
  );
  // realPathIndex leaves out a name it cannot resolve and one the lexical check refuses.
  assert.equal(realPathIndex(model, ["swc/Missing.yaml", "../x.yaml"]).size, 0);
});

/** The outline of *lines* as a file of *layer*, through the shipped reference table. */
function outlineOfLines(lines: string[], layer = "swc"): OutlineSymbol[] {
  const refs = loadReferences(fs.readFileSync(path.join(SCHEMAS_DIR, "references.json"), "utf8"));
  return outlineOf(refs, `${lines.join("\n")}\n`, layer);
}

test("outlineRoot and outlineGroups: the element when its identity line was read, else the groups on top", () => {
  const ports = symbol("ports", 3, [symbol("pSpeed", 4)]);
  const root = symbol("Sensor", 1, [ports], 0);
  assert.equal(outlineRoot([root]), root);
  assert.deepEqual(outlineGroups([root]), [ports]);
  // No root: a top level of groups, a lone symbol not starting at line 0, an empty outline.
  const rootless = [symbol("ports", 2), symbol("runnables", 6)];
  assert.equal(outlineRoot(rootless), undefined);
  assert.equal(outlineGroups(rootless), rootless);
  assert.equal(outlineRoot([symbol("ports", 2)]), undefined);
  assert.deepEqual(outlineGroups([]), []);
});

/** Every committed golden set's index page, by the set's directory name. */
function goldenIndexes(): Array<[string, string]> {
  const root = path.dirname(GOLDEN);
  return fs
    .readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry): [string, string] => [
      entry.name,
      fs.readFileSync(path.join(root, entry.name, "index.html"), "utf8"),
    ]);
}

test("the cursor's member groups are the generator's member groups less delegations", () => {
  // The generator's own output decides it: every member the committed golden indexes list under
  // an element is `<group>/<name>`, and the extension names the groups a cursor can stand in. A
  // delegation is the composition's own line, not a member drawn as part of an element (only
  // ports and runnables are): a cursor inside one reveals the composition, so `delegations` is the
  // one group left out. Not reached: a group the generator can write that no golden model
  // exercises.
  const written = new Set<string>();
  for (const [name, html] of goldenIndexes()) {
    const block = parseElementsBlock(html);
    assert.equal(block.kind, "ok", name);
    for (const element of block.kind === "ok" ? block.value.values() : []) {
      for (const member of Object.keys(element.members)) {
        written.add(member.split("/")[0]);
      }
    }
  }
  assert.deepEqual([...written].sort(), ["delegations", "ports", "runnables"]);
  assert.deepEqual(
    [...DRAWN_MEMBER_GROUPS].sort(),
    [...written].filter((group) => group !== "delegations").sort(),
  );
});

test("memberAtLine: the port or runnable directly beneath the root's group, from any line inside it", () => {
  const lines = [
    "name: Sensor", //                0
    "type: application", //           1
    "ports:", //                      2
    "  - name: pSpeed", //            3
    "    direction: provide", //      4
    "    interface: ISpeed", //       5
    "    com_specs:", //              6
    "      - element: speed", //      7
    "        init: 0", //             8
    "  - name: pRaw", //              9
    "    direction: provide", //     10
    "    interface: ISpeed", //      11
    "runnables:", //                 12
    "  - name: SensorRun", //        13
    "    events:", //                14
    "      - type: timing", //       15
    "        period: 0.01", //       16
    "    writes:", //                17
    "      - port: pSpeed", //       18
    "        element: speed", //     19
    "handle_termination_and_restart: no_support", // 20
    "satisfies:", //                 21
    "- REQ_DIAGRAMS_001@1", //       22
  ];
  const outline = outlineOfLines(lines);
  const at = (line: number) => memberAtLine(outline, line);
  assert.equal(at(3), "ports/pSpeed", "the member's own line");
  assert.equal(at(5), "ports/pSpeed", "a field of the member");
  assert.equal(at(7), "ports/pSpeed", "a com-spec entry gives its port");
  assert.equal(at(8), "ports/pSpeed");
  assert.equal(at(9), "ports/pRaw");
  assert.equal(at(13), "runnables/SensorRun");
  assert.equal(at(15), "runnables/SensorRun", "a nested event gives its runnable");
  assert.equal(at(18), "runnables/SensorRun", "an access entry gives its runnable");
  // Anywhere else: the element.
  for (const line of [0, 1, 2, 12, 20, 22, 99]) {
    assert.equal(at(line), null, `line ${line}`);
  }
  // A flow-style collection is not split by the outline: the element.
  const flow = outlineOfLines(["name: A", "ports: [{name: p, direction: provide}]"]);
  assert.equal(memberAtLine(flow, 1), null);
  // A composition's delegation is no member the cursor names (only ports and runnables).
  const composition = outlineOfLines(
    ["name: Chassis", "delegations:", "  - name: dSpeed", "    port: pSpeedOut"],
    "compositions",
  );
  assert.equal(memberAtLine(composition, 2), null);
  // An outline with no root (no identity line): its top-level groups are the root's.
  const rootless = outlineOfLines(["type: application", "ports:", "  - name: pIn"]);
  assert.equal(memberAtLine(rootless, 2), "ports/pIn");
  // A member whose identity the outline could not read is not named.
  const unnamed = [symbol("A", 0, [symbol("ports", 1, [symbol("[0]", 2)])], 0)];
  unnamed[0].range.endLine = 3;
  unnamed[0].children[0].range.endLine = 3;
  unnamed[0].children[0].children[0].range.endLine = 3;
  assert.equal(memberAtLine(unnamed, 2), null);
});

test("chooseView: the tab's view when it draws the element, else the one view, else a pick", () => {
  assert.deepEqual(chooseView([], "ibd-Root"), { kind: "none" });
  assert.deepEqual(chooseView(["ibd-Root"], undefined), { kind: "view", view: "ibd-Root" });
  assert.deepEqual(chooseView(["ibd-Root"], "deployment"), { kind: "view", view: "ibd-Root" });
  assert.deepEqual(chooseView(["ibd-Root", "deployment"], "deployment"), {
    kind: "view",
    view: "deployment",
  });
  assert.deepEqual(chooseView(["ibd-Root", "deployment"], undefined), {
    kind: "pick",
    views: ["ibd-Root", "deployment"],
  });
  assert.deepEqual(chooseView(["ibd-Root", "deployment"], "nesting"), {
    kind: "pick",
    views: ["ibd-Root", "deployment"],
  });
});

test("Show in Diagram's lines, the reveal's name and the lens's argument", () => {
  assert.deepEqual([...DIAGRAM_EDITOR_COMMANDS], ["swcraft.showInDiagram"]);
  assert.equal(GENERATE_DIAGRAMS_BUTTON, "Generate Diagrams");
  assert.equal(notOnAnyDiagramLine("Sensor"), "Sensor is not on any diagram as last generated");
  assert.equal(
    SHOW_IN_DIAGRAM_NO_ELEMENT_LINE,
    "SWCraft: Show in Diagram shows the element of a model file: open one in the editor first.",
  );
  assert.match(noDiagramsLine("Sensor"), /^Sensor cannot be shown: no diagrams/);
  assert.equal(pickPlaceholder("Sensor", 3), "Sensor is on 3 diagrams — which one?");
  assert.equal(revealName("Sensor", null), "Sensor");
  assert.equal(revealName("Sensor", "ports/pSpeed"), "Sensor.pSpeed");
  assert.equal(revealName("Ctl", "runnables/Run"), "Ctl.Run");
  assert.deepEqual(showInDiagramArgument({ file: "swc/chassis/Sensor.yaml", name: "Sensor" }), {
    file: "swc/chassis/Sensor.yaml",
    name: "Sensor",
  });
  assert.deepEqual(showInDiagramArgument({ file: "tasks/T~1.yaml" }), {
    file: "tasks/T~1.yaml",
    name: "T~1",
  });
  for (const arg of [
    undefined,
    null,
    "swc/A.yaml",
    { view: "ibd-Root" },
    { file: "/abs/A.yaml" },
    { file: "../A.yaml" },
    { file: "swc\\A.yaml" },
    { file: "swc/A.txt" },
    { file: 3 },
  ]) {
    assert.equal(showInDiagramArgument(arg), undefined, JSON.stringify(arg));
  }
});

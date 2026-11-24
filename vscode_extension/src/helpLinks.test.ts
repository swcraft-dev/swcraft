// The guide links: the rule link's URI built and parsed back, the forged links the handler
// refuses, which notification gets *Learn more*, the reading of a diagnostic's code in both
// forms, and the two hand-written anchors declared in their guide pages. No `vscode` import:
// this runs under plain `node --test`.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import type { RunOutput } from "./commandCore";
import {
  diagnosticRule,
  guideCommandArg,
  guidePageUrl,
  guideTabFilter,
  guideTabOpen,
  HELP_PAGES,
  HELP_TOPICS,
  helpTopicForRun,
  helpUri,
  INTEGRATED_BROWSER_OPEN,
  MODEL_URI_PATH,
  modelLinkAction,
  NO_PROJECT_FOR_MODEL_LINK,
  PIN_REFUSAL_PREFIX,
  parseHelpUri,
  parseModelUri,
  queryValues,
  RULES_PAGE,
  RuleLinkResolver,
  refusedModelLinkLine,
  SECTION_ID_LIMIT,
  SIMPLE_BROWSER_SHOW,
  sectionPagePath,
  type UriParts,
  uriRoute,
} from "./helpLinks";
import { CONTROL_CHARACTERS, channelQuote, LINK_QUOTE_LIMIT } from "./storageName";
import { readCliPackageSource, readSource } from "./testSupport";

const EXTENSION_ID = "swcraft-local.swcraft-vscode";

test("a rule link is built on the editor's own scheme and parsed back to its page and anchor", () => {
  for (const scheme of ["vscode", "vscode-insiders", "a-fork"]) {
    const link = helpUri(scheme, EXTENSION_ID, RULES_PAGE, "trace.link-needs-confirmation");
    assert.deepEqual(link, {
      scheme,
      authority: EXTENSION_ID,
      path: "/guide",
      query: "page=reference%2Frules%2F&anchor=trace.link-needs-confirmation",
    });
    assert.ok(link !== undefined);
    assert.deepEqual(parseHelpUri(link, EXTENSION_ID), {
      kind: "open",
      page: RULES_PAGE,
      anchor: "trace.link-needs-confirmation",
    });
  }
});

test("a link the desktop mapping appended its window id to still parses to its page and anchor", () => {
  // VS Code 1.143.1's desktop `asExternalUri` appends `&windowId=<n>` to the
  // link's query (read from its shipped workbench code); the handler reads
  // only page and anchor.
  const link = helpUri("vscode", EXTENSION_ID, RULES_PAGE, "swc.port-unconnected");
  assert.ok(link !== undefined);
  const mapped: UriParts = { ...link, query: `${link.query}&windowId=7` };
  assert.deepEqual(parseHelpUri(mapped, EXTENSION_ID), {
    kind: "open",
    page: RULES_PAGE,
    anchor: "swc.port-unconnected",
  });
});

test("a link survives its round trip through a URI string", () => {
  // Production hands the link to `vscode.Uri.from()`, Problems keeps it, and VS Code reparses
  // it before the handler sees it. `Uri.toString()` (VS Code 1.143.1's shipped code)
  // percent-encodes every reserved character of the query — `=`, `&` and the `%` of an
  // already-encoded `/` included — as `encodeURIComponent` does, and `Uri.parse` decodes the
  // query once. This transcribes those two steps; the real round trip is not reached here.
  const link = helpUri("vscode", EXTENSION_ID, RULES_PAGE, "trace.requirement-stub");
  assert.ok(link !== undefined);
  const text = `${link.scheme}://${link.authority}${link.path}?${encodeURIComponent(link.query)}`;
  assert.equal(
    text,
    `vscode://${EXTENSION_ID}/guide?page%3Dreference%252Frules%252F%26anchor%3Dtrace.requirement-stub`,
  );
  const parsed = new URL(text);
  const reparsed = {
    authority: parsed.host,
    path: parsed.pathname,
    query: decodeURIComponent(parsed.search.slice(1)),
  };
  assert.deepEqual(parseHelpUri(reparsed, EXTENSION_ID), {
    kind: "open",
    page: RULES_PAGE,
    anchor: "trace.requirement-stub",
  });
});

test("helpUri builds no link the handler would refuse", () => {
  assert.equal(helpUri("vscode", EXTENSION_ID, RULES_PAGE, "has space"), undefined);
  assert.equal(helpUri("vscode", EXTENSION_ID, RULES_PAGE, ""), undefined);
  assert.equal(helpUri("vscode", EXTENSION_ID, RULES_PAGE, "a".repeat(129)), undefined);
  assert.equal(helpUri("vscode", EXTENSION_ID, "../../etc/", "x"), undefined);
  const noAnchor = helpUri("vscode", EXTENSION_ID, HELP_TOPICS.pin.page);
  assert.ok(noAnchor !== undefined);
  assert.equal(new URLSearchParams(noAnchor.query).has("anchor"), false);
  assert.deepEqual(parseHelpUri(noAnchor, EXTENSION_ID), {
    kind: "open",
    page: HELP_TOPICS.pin.page,
    anchor: undefined,
  });
});

test("a forged link is refused: another extension, another path, a foreign page, a bad anchor", () => {
  const good = { authority: EXTENSION_ID, path: "/guide", query: `page=${RULES_PAGE}&anchor=a.b` };
  assert.equal(parseHelpUri(good, EXTENSION_ID).kind, "open");
  // VS Code compares extension ids case-insensitively.
  assert.equal(
    parseHelpUri({ ...good, authority: EXTENSION_ID.toUpperCase() }, EXTENSION_ID).kind,
    "open",
  );
  const forged = [
    { ...good, authority: "someone.else" },
    { ...good, path: "/guide/" },
    { ...good, path: "/run" },
    { ...good, query: "anchor=a.b" },
    { ...good, query: "page=https%3A%2F%2Fexample.com%2F&anchor=a.b" },
    { ...good, query: "page=reference%2Frules%2F..%2F..%2F&anchor=a.b" },
    { ...good, query: `page=${RULES_PAGE}&page=vscode%2F` },
    { ...good, query: `page=${RULES_PAGE}&anchor=%22%3E%3Cscript%3E` },
    { ...good, query: `page=${RULES_PAGE}&anchor=` },
    { ...good, query: `page=${RULES_PAGE}&anchor=a&anchor=b` },
    { ...good, query: `page=${RULES_PAGE}&anchor=${"a".repeat(129)}` },
  ];
  for (const uri of forged) {
    const request = parseHelpUri(uri, EXTENSION_ID);
    assert.equal(request.kind, "refused", JSON.stringify(uri));
  }
});

/** The `guide` tool's link of *id*, as `swcraft_tools.guide_search.section_link()` composes it. */
const sectionLink = (id: string): string => `vscode://${EXTENSION_ID}/guide?section=${id}`;

/** The parts the handler reads of *link*, as `vscode.Uri.parse` gives them: the query and fragment decoded. */
function uriOf(link: string): Pick<UriParts, "authority" | "path" | "query" | "fragment"> {
  const url = new URL(link);
  return {
    authority: url.host,
    path: url.pathname,
    query: decodeURIComponent(url.search.slice(1)),
    fragment: decodeURIComponent(url.hash.slice(1)),
  };
}

test("a section link opens the section's page of the served guide at its anchor", () => {
  const cases: [string, string, string | undefined][] = [
    [
      "how-to/connect-components#before-you-start",
      "how-to/connect-components/",
      "before-you-start",
    ],
    ["how-to/connect-components", "how-to/connect-components/", undefined],
    ["how-to", "how-to/", undefined],
    ["reference/model/swc#runnables", "reference/model/swc/", "runnables"],
    ["index", "", undefined],
    ["index#what-swcraft-is-not", "", "what-swcraft-is-not"],
  ];
  for (const [id, page, anchor] of cases) {
    assert.deepEqual(
      parseHelpUri(uriOf(sectionLink(id)), EXTENSION_ID),
      { kind: "open", page, anchor },
      id,
    );
  }
  assert.equal(
    guidePageUrl("http://127.0.0.1:5000/", "how-to/connect-components/", "before-you-start"),
    "http://127.0.0.1:5000/how-to/connect-components/#before-you-start",
  );
});

test("a section link survives its round trip through a URI string, with and without a fragment", () => {
  // The chat hands the link to VS Code as text: `Uri.parse` decodes the query and the fragment
  // once, `Uri.toString()` (VS Code 1.143.1's shipped code) percent-encodes them as
  // `encodeURIComponent` does, and the handler sees that string parsed again. This transcribes
  // those steps; the real round trip is not reached here.
  const cases: [string, string, string | undefined][] = [
    [
      "how-to/connect-components#before-you-start",
      "how-to/connect-components/",
      "before-you-start",
    ],
    ["how-to/connect-components", "how-to/connect-components/", undefined],
  ];
  for (const [id, page, anchor] of cases) {
    const first = uriOf(sectionLink(id));
    const fragment = first.fragment ? `#${encodeURIComponent(first.fragment)}` : "";
    const text = `vscode://${first.authority}${first.path}?${encodeURIComponent(first.query)}${fragment}`;
    assert.equal(
      text,
      `vscode://${EXTENSION_ID}/guide?section%3Dhow-to%2Fconnect-components${anchor ? `#${anchor}` : ""}`,
    );
    const reparsed = uriOf(text);
    assert.deepEqual(reparsed, first, id);
    assert.deepEqual(parseHelpUri(reparsed, EXTENSION_ID), { kind: "open", page, anchor }, id);
  }
});

test("a section link whose anchor came encoded in the query parses the same", () => {
  const encoded = uriOf(sectionLink("how-to/connect-components%23before-you-start"));
  assert.equal(encoded.fragment, "");
  assert.deepEqual(parseHelpUri(encoded, EXTENSION_ID), {
    kind: "open",
    page: "how-to/connect-components/",
    anchor: "before-you-start",
  });
});

test("a section link's page maps to the site's directory URL", () => {
  assert.equal(sectionPagePath("index"), "");
  assert.equal(sectionPagePath("how-to"), "how-to/");
  assert.equal(sectionPagePath("reference/cli"), "reference/cli/");
});

test("a forged section link is refused: no path, no URL, no second section, nothing outside the id alphabet", () => {
  const good = uriOf(sectionLink("how-to/connect-components#before-you-start"));
  assert.equal(parseHelpUri(good, EXTENSION_ID).kind, "open");
  const forged: Pick<UriParts, "authority" | "path" | "query" | "fragment">[] = [
    { ...good, authority: "someone.else" },
    { ...good, path: "/guide/x" },
    { ...good, query: "section=../../etc/passwd" },
    { ...good, query: "section=how-to/../../etc" },
    { ...good, query: "section=/etc/passwd" },
    { ...good, query: "section=how-to/" },
    { ...good, query: "section=how-to//connect-components" },
    { ...good, query: "section=how-to\\connect-components" },
    { ...good, query: "section=C:/Windows" },
    { ...good, query: "section=https://example.com/" },
    { ...good, query: "section=How-To/Connect" },
    { ...good, query: "section=how-to/connect components" },
    { ...good, query: "section=how-to.md" },
    { ...good, query: "section=" },
    { ...good, query: "section=how-to&section=cli" },
    { ...good, query: `section=how-to&page=${RULES_PAGE}` },
    { ...good, query: "section=how-to&anchor=x" },
    { ...good, query: "section=how-to#a", fragment: "b" },
    { ...good, query: "section=how-to#a#b", fragment: "" },
    { ...good, fragment: "a.b" },
    { ...good, fragment: "%22%3E%3Cscript%3E" },
    { ...good, fragment: "../x" },
    { ...good, query: `section=${"a".repeat(SECTION_ID_LIMIT + 1)}`, fragment: "" },
    // The bound is the complete id's: an anchor in the fragment counts.
    { ...good, query: "section=index", fragment: "a".repeat(1_000_000) },
    {
      ...good,
      query: "section=index",
      fragment: "a".repeat(SECTION_ID_LIMIT - "index#".length + 1),
    },
  ];
  for (const uri of forged) {
    assert.equal(parseHelpUri(uri, EXTENSION_ID).kind, "refused", JSON.stringify(uri));
  }
  assert.equal(
    parseHelpUri(
      { ...good, query: `section=${"a".repeat(SECTION_ID_LIMIT)}`, fragment: "" },
      EXTENSION_ID,
    ).kind,
    "open",
  );
  const longestAnchor = "a".repeat(SECTION_ID_LIMIT - "index#".length);
  for (const uri of [
    { ...good, query: "section=index", fragment: longestAnchor },
    { ...good, query: `section=index%23${longestAnchor}`, fragment: "" },
  ]) {
    assert.deepEqual(parseHelpUri(uri, EXTENSION_ID), {
      kind: "open",
      page: sectionPagePath("index"),
      anchor: longestAnchor,
    });
  }
});

test("guideCommandArg: a section page and anchor open the served page at that anchor", () => {
  assert.deepEqual(
    guideCommandArg({ page: "reference/model/interfaces", anchor: "sender_receiver" }),
    {
      page: "reference/model/interfaces/",
      anchor: "sender_receiver",
    },
  );
  assert.deepEqual(guideCommandArg({ page: "reference/model/swc" }), {
    page: "reference/model/swc/",
    anchor: undefined,
  });
  assert.deepEqual(guideCommandArg({ page: "index", anchor: "what-swcraft-is-not" }), {
    page: "",
    anchor: "what-swcraft-is-not",
  });
  const longestAnchor = "a".repeat(SECTION_ID_LIMIT - "index#".length);
  assert.deepEqual(guideCommandArg({ page: "index", anchor: longestAnchor }), {
    page: "",
    anchor: longestAnchor,
  });
  assert.deepEqual(guideCommandArg({ page: "a".repeat(SECTION_ID_LIMIT) }), {
    page: `${"a".repeat(SECTION_ID_LIMIT)}/`,
    anchor: undefined,
  });
});

test("guideCommandArg: anything but a section page and anchor is the guide's front page", () => {
  const good = { page: "reference/model/interfaces", anchor: "sender_receiver" };
  const refused: unknown[] = [
    undefined,
    null,
    "reference/model/interfaces",
    42,
    ["reference/model/interfaces"],
    {},
    // A panel TreeItem: a label and a command, no page.
    { label: "Open Guide", command: { command: "swcraft.openGuide", title: "Open Guide" } },
    { ...good, page: "../reference/model/interfaces" },
    { ...good, page: "reference/../../etc" },
    { ...good, page: "/reference/model/interfaces" },
    { ...good, page: "reference\\model\\interfaces" },
    { ...good, page: "https://example.com/" },
    { ...good, page: "file:///etc/passwd" },
    { ...good, page: "reference/model/" },
    { ...good, page: "Reference/Model" },
    { ...good, page: "reference/model/interfaces.md" },
    { ...good, page: "" },
    { ...good, page: 7 },
    { ...good, anchor: "sender.receiver" },
    { ...good, anchor: "a/b" },
    { ...good, anchor: "../x" },
    { ...good, anchor: "%22%3E%3Cscript%3E" },
    { ...good, anchor: "" },
    { ...good, anchor: null },
    { ...good, anchor: 1 },
    { page: "a".repeat(SECTION_ID_LIMIT + 1) },
    { page: "index", anchor: "a".repeat(SECTION_ID_LIMIT - "index#".length + 1) },
  ];
  for (const raw of refused) {
    assert.equal(guideCommandArg(raw), undefined, JSON.stringify(raw));
  }
});

test("the help pages are the rules page and the two sections' pages, nothing else", () => {
  assert.deepEqual([...HELP_PAGES].sort(), [RULES_PAGE, "upgrading/", "vscode/"].sort());
  assert.equal(RULES_PAGE, "reference/rules/");
});

test("guidePageUrl joins the served root, the page and the anchor", () => {
  assert.equal(
    guidePageUrl("http://127.0.0.1:5000/", RULES_PAGE, "trace.requirement-stub"),
    "http://127.0.0.1:5000/reference/rules/#trace.requirement-stub",
  );
  assert.equal(guidePageUrl("http://127.0.0.1:5000/", "", undefined), "http://127.0.0.1:5000/");
  assert.equal(
    guidePageUrl("http://127.0.0.1:5000/", HELP_TOPICS.pin.page, HELP_TOPICS.pin.anchor),
    "http://127.0.0.1:5000/upgrading/#the-procedure",
  );
});

const PIN_LINE =
  "Error: this project's swcraft.yaml pins toolchain_version 9.9.9 and this is SWCraft 0.20.0 — " +
  "install the 9.9.9 workstation archive (its swcraft_install.cmd on Windows, swcraft_install.sh " +
  "elsewhere), or edit the pin if this project is meant to move to this release.";

const failed = (stderr: string, spawnFailed = false): RunOutput => ({
  exitCode: spawnFailed ? null : 1,
  stderr,
  spawnFailed,
});

test("helpTopicForRun: a run refused for the pin opens the upgrading section, on any line of stderr", () => {
  assert.equal(helpTopicForRun(failed(`${PIN_LINE}\n`)), "pin");
  assert.equal(helpTopicForRun(failed(`$ swcraft clean-outputs\r\n${PIN_LINE}\r\n`)), "pin");
});

test("helpTopicForRun: the pin's Warning line, another failure and a spawn failure get no button", () => {
  const warning = PIN_LINE.replace(/^Error:/, "Warning:");
  assert.equal(helpTopicForRun(failed(warning)), undefined);
  assert.equal(helpTopicForRun(failed("Error: model/swc/a.yaml: not a mapping\n")), undefined);
  // The pin's sentence quoted inside another line is not the refusal.
  assert.equal(helpTopicForRun(failed(`note: ${PIN_LINE}`)), undefined);
  assert.equal(helpTopicForRun(failed(PIN_LINE, true)), undefined);
  // A timed-out run was started: its stderr is read like any other.
  assert.equal(helpTopicForRun({ exitCode: null, stderr: PIN_LINE, spawnFailed: false }), "pin");
});

test("PIN_REFUSAL_PREFIX is the opening of check_toolchain_pin's refusal", () => {
  // packages/tools/tests/test_cli.py asserts the CLI's refusal line starts with this
  // text; this side reads the two halves of that line off the Python source —
  // the `Error: ` cli/project_args.py's check_toolchain_pin prints the refusal with, and the
  // sentence's fixed opening before the pin, which toolchain_pin.py's
  // toolchain_pin_problem builds — and asserts the constant is exactly their join.
  const cli = readCliPackageSource();
  const leaf = readSource("../packages/tools/src/swcraft_tools/toolchain_pin.py");
  assert.match(cli, /def check_toolchain_pin\(/);
  assert.match(leaf, /def toolchain_pin_problem\(/);
  const printed = cli.match(/print\(f"(Error: )\{problem\}", file=sys\.stderr\)/);
  const opening = leaf.match(
    /f"(this project's swcraft\.yaml pins toolchain_version) \{pin\} and this is/,
  );
  assert.ok(printed !== null, "the cli package's refusal prints the problem after `Error: `");
  assert.ok(opening !== null, "toolchain_pin.py's sentence is read up to the pin");
  assert.equal(PIN_REFUSAL_PREFIX, `${printed[1]}${opening[1]}`);
});

test("diagnosticRule reads the rule from a plain code and from a linked one", () => {
  assert.equal(diagnosticRule("trace.link-needs-confirmation"), "trace.link-needs-confirmation");
  assert.equal(
    diagnosticRule({ value: "trace.link-needs-confirmation", target: { scheme: "vscode" } }),
    "trace.link-needs-confirmation",
  );
  assert.equal(diagnosticRule(undefined), undefined);
  assert.equal(diagnosticRule(42), undefined);
  assert.equal(diagnosticRule({ value: 42, target: undefined }), undefined);
});

test("each help topic's anchor is declared exactly once in its guide page", () => {
  for (const [name, { page, anchor }] of Object.entries(HELP_TOPICS)) {
    const file = `../docs/guide/${page.replace(/\/$/, "")}.md`;
    const text = readSource(file);
    const declared = text.split(`{ #${anchor} }`).length - 1;
    assert.equal(declared, 1, `${name}: ${file} declares { #${anchor} } ${declared} times`);
    assert.match(
      text,
      new RegExp(`^## .+ \\{ #${anchor.replace(/[.-]/g, "\\$&")} \\}$`, "m"),
      `${name}: the anchor is a heading's`,
    );
  }
});

test("one guide tab per window: the integrated browser reuses a tab of the served guide", () => {
  const address = "http://127.0.0.1:57429/reference/rules/#trace.link-needs-confirmation";
  const open = guideTabOpen(
    ["simpleBrowser.show", INTEGRATED_BROWSER_OPEN, "x"],
    address,
    RULES_PAGE,
  );
  assert.equal(open.command, "workbench.action.browser.open");
  assert.deepEqual(open.args, [{ url: address, reuseUrlFilter: "http://127.0.0.1:57429/**" }]);
});

test("one guide tab per window: without the integrated browser, the Simple Browser's one view", () => {
  const address = "http://127.0.0.1:57429/vscode/#turning-a-folder-into-a-project";
  const page = HELP_TOPICS.project.page;
  assert.deepEqual(guideTabOpen(["simpleBrowser.show"], address, page), {
    command: SIMPLE_BROWSER_SHOW,
    args: [address],
  });
  assert.equal(SIMPLE_BROWSER_SHOW, "simpleBrowser.show");
  // The decision does not depend on the Simple Browser being listed: absent, the
  // run throws and `openGuidePage()` falls back to the system browser.
  assert.equal(guideTabOpen([], address, page).command, SIMPLE_BROWSER_SHOW);
});

test("the guide tab filter is the served guide's root, whatever page or anchor", () => {
  for (const [address, page] of [
    ["http://127.0.0.1:57429/", ""],
    ["http://127.0.0.1:57429/reference/rules/#a.b", RULES_PAGE],
    ["http://127.0.0.1:57429/upgrading/#the-procedure", HELP_TOPICS.pin.page],
  ]) {
    assert.equal(guideTabFilter(address, page), "http://127.0.0.1:57429/**");
  }
  // Another port is another guide server: a tab on it is not this guide's.
  assert.equal(
    guideTabFilter("http://127.0.0.1:60001/vscode/", "vscode/"),
    "http://127.0.0.1:60001/**",
  );
  // A mapped remote address keeps its own origin, scheme included.
  assert.equal(
    guideTabFilter("https://name-57429.app.github.dev/reference/rules/#x", RULES_PAGE),
    "https://name-57429.app.github.dev/**",
  );
});

test("the guide tab filter keeps a path-based proxy's prefix, so two ports on one origin stay apart", () => {
  // `code serve-web` and vscode.dev map a forwarded port under one origin as
  // `/proxy/<port>/`: the filter names this guide's root there, not the origin.
  assert.equal(
    guideTabFilter("https://host.example/proxy/57429/reference/rules/#a.b", RULES_PAGE),
    "https://host.example/proxy/57429/**",
  );
  assert.equal(
    guideTabFilter("https://host.example/proxy/57429/", ""),
    "https://host.example/proxy/57429/**",
  );
  assert.notEqual(
    guideTabFilter("https://host.example/proxy/60001/vscode/", "vscode/"),
    guideTabFilter("https://host.example/proxy/57429/vscode/", "vscode/"),
  );
  // A mapping that rewrote the page's own path: the filter is that whole path,
  // so it reuses only that page's tab (the limit guideTabFilter's comment names).
  assert.equal(
    guideTabFilter("https://host.example/elsewhere/#a.b", RULES_PAGE),
    "https://host.example/elsewhere/**",
  );
});

// The rule-link resolver: which mapped links are kept, the run's wait, and the
// per-call limit that drops a call that never settles.
interface FakeLink {
  readonly scheme: string;
  readonly id: number;
}

function countingMap(scheme: (rule: string) => string): {
  map: (rule: string) => Promise<FakeLink>;
  calls: string[];
} {
  const calls: string[] = [];
  return {
    calls,
    map: (rule) => {
      calls.push(rule);
      return Promise.resolve({ scheme: scheme(rule), id: calls.length });
    },
  };
}

test("a link mapped to the product scheme (the desktop's) is kept and reused by later runs", async () => {
  const fake = countingMap(() => "vscode");
  const resolver = new RuleLinkResolver<FakeLink>("vscode", fake.map);
  const first = await resolver.resolve(["a", "a", "b"], 50, () => assert.fail("no line"));
  const second = await resolver.resolve(["a", "b"], 50, () => assert.fail("no line"));
  assert.deepEqual(fake.calls, ["a", "b"], "one call per rule for the window");
  assert.equal(second.get("a"), first.get("a"), "the very same mapped link, unaltered");
  assert.equal(second.get("b"), first.get("b"));
});

test("a link mapped to another scheme (a web workbench's) serves one run and is mapped again by the next", async () => {
  const fake = countingMap(() => "https");
  const resolver = new RuleLinkResolver<FakeLink>("vscode", fake.map);
  const first = await resolver.resolve(["a"], 50, () => assert.fail("no line"));
  const second = await resolver.resolve(["a"], 50, () => assert.fail("no line"));
  assert.deepEqual(fake.calls, ["a", "a"], "a fresh call per run");
  assert.notEqual(first.get("a"), undefined);
  assert.notEqual(second.get("a")?.id, first.get("a")?.id, "never the previous run's link");
});

test("a rule no link can name, a throwing map and a rejected call leave the rule unlinked and are asked again", async () => {
  const calls: string[] = [];
  const resolver = new RuleLinkResolver<FakeLink>("vscode", (rule) => {
    calls.push(rule);
    if (rule === "none") {
      return undefined;
    }
    if (rule === "throws") {
      throw new Error("boom");
    }
    return Promise.reject(new Error("refused"));
  });
  for (let run = 0; run < 2; run += 1) {
    const links = await resolver.resolve(["none", "throws", "rejects"], 50, () =>
      assert.fail("a rejection writes no line"),
    );
    assert.equal(links.size, 0);
  }
  assert.deepEqual(calls, ["none", "throws", "rejects", "none", "throws", "rejects"]);
});

test("a run waits for its calls at most its wait; a late desktop answer is kept for the next run", async () => {
  let answer: ((link: FakeLink) => void) | undefined;
  let calls = 0;
  const resolver = new RuleLinkResolver<FakeLink>(
    "vscode",
    () => {
      calls += 1;
      return new Promise<FakeLink>((resolve) => {
        answer = resolve;
      });
    },
    1_000,
  );
  const first = await resolver.resolve(["a"], 5, () => assert.fail("no line"));
  assert.equal(first.size, 0, "the run published without it");
  answer?.({ scheme: "vscode", id: 1 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(first.size, 0, "the late answer adds nothing to the earlier run's links");
  const second = await resolver.resolve(["a"], 5, () => assert.fail("no line"));
  assert.equal(second.get("a")?.id, 1);
  assert.equal(calls, 1);
});

test("a call that never settles is given up at its limit, logged once, and the next run issues a fresh call", async () => {
  const lines: string[] = [];
  let calls = 0;
  const resolver = new RuleLinkResolver<FakeLink>(
    "vscode",
    () => {
      calls += 1;
      return calls === 1
        ? new Promise<FakeLink>(() => undefined)
        : Promise.resolve({ scheme: "vscode", id: calls });
    },
    20,
  );
  const first = await resolver.resolve(["swc.port-unconnected"], 5, (line) => lines.push(line));
  assert.equal(first.size, 0);
  // Before the limit, a run shares the call in flight.
  await resolver.resolve(["swc.port-unconnected"], 1, (line) => lines.push(line));
  assert.equal(calls, 1);
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.deepEqual(lines, [
    "SWCraft: the guide link of rule swc.port-unconnected was not resolved within 0.02 s; " +
      "it is shown as its plain name, and the next check asks again.",
  ]);
  const next = await resolver.resolve(["swc.port-unconnected"], 50, (line) => lines.push(line));
  assert.equal(calls, 2, "a fresh call, not the dropped one");
  assert.equal(next.get("swc.port-unconnected")?.id, 2);
  assert.equal(lines.length, 1, "logged once");
});

// --- the model link: vscode://<extension id>/model?path=<storage name> ----------------

/**
 * A model link as VS Code hands it over: `vscode.Uri` keeps its components
 * percent-decoded, so *query* is the decoded one — a page's
 * `?path=swc%2Fchassis%2FSensor.yaml` arrives as `path=swc/chassis/Sensor.yaml`.
 */
function modelLink(query: string, authority: string = EXTENSION_ID, path: string = MODEL_URI_PATH) {
  return { scheme: "vscode", authority, path, query, fragment: "" };
}

/** What VS Code does to a link's raw query before the handler sees it. */
function decodedByVsCode(rawQuery: string): string {
  return decodeURIComponent(rawQuery);
}

test("parseModelUri opens a storage name, the needs' swcraft_path urlencoded as a team page writes it", () => {
  assert.deepEqual(
    parseModelUri(modelLink(decodedByVsCode("path=swc%2Fchassis%2FSensor.yaml")), EXTENSION_ID),
    { kind: "open", path: "swc/chassis/Sensor.yaml" },
  );
  assert.deepEqual(
    parseModelUri(modelLink("path=swc/chassis/Sensor.yaml&windowId=3"), EXTENSION_ID.toUpperCase()),
    {
      kind: "open",
      path: "swc/chassis/Sensor.yaml",
    },
  );
});

test("parseModelUri refuses another extension, another path, no single path and every name the storage-name rule refuses", () => {
  assert.equal(
    parseModelUri(modelLink("path=a.yaml", "other.extension"), EXTENSION_ID).kind,
    "refused",
  );
  assert.equal(
    parseModelUri(modelLink("path=a.yaml", EXTENSION_ID, "/guide"), EXTENSION_ID).kind,
    "refused",
  );
  assert.equal(
    parseModelUri(modelLink("path=a.yaml", EXTENSION_ID, "/model/"), EXTENSION_ID).kind,
    "refused",
  );
  assert.equal(parseModelUri(modelLink(""), EXTENSION_ID).kind, "refused");
  assert.equal(parseModelUri(modelLink("path=a.yaml&path=b.yaml"), EXTENSION_ID).kind, "refused");
  for (const name of [
    "%2Fetc%2Fx.yaml",
    "C%3A%2Fx.yaml",
    "%2F%2Fserver%2Fshare%2Fx.yaml",
    "swc%5Ca.yaml",
    "..%2Fx.yaml",
    "a%2F..%2Fx.yaml",
    "a%2F%2Fb.yaml",
    "a%00.yaml",
    "a.txt",
    "",
  ]) {
    const request = parseModelUri(modelLink(decodedByVsCode(`path=${name}`)), EXTENSION_ID);
    assert.equal(request.kind, "refused", name);
  }
  assert.equal(
    parseHelpUri(modelLink("path=a.yaml"), EXTENSION_ID).kind,
    "refused",
    "the guide route takes no model link",
  );
});

test("parseModelUri decodes the query VS Code already decoded never again: a + and a %2F stay", () => {
  // A page's `a%2Bb.yaml` arrives as `a+b.yaml`; a second decoding read the + as a space.
  assert.deepEqual(parseModelUri(modelLink(decodedByVsCode("path=a%2Bb.yaml")), EXTENSION_ID), {
    kind: "open",
    path: "a+b.yaml",
  });
  // `%252F` arrives as `%2F`: a file named so, never the separator a second decoding made of it.
  assert.deepEqual(parseModelUri(modelLink(decodedByVsCode("path=a%252Fb.yaml")), EXTENSION_ID), {
    kind: "open",
    path: "a%2Fb.yaml",
  });
  // What it does not reach: a %26 is an & before the split, and splits the parameter.
  assert.deepEqual(parseModelUri(modelLink(decodedByVsCode("path=a%26b.yaml")), EXTENSION_ID), {
    kind: "refused",
    reason: "the name is not a .yaml file",
    path: "a",
  });
  assert.deepEqual(queryValues("path=a=b.yaml&x&path", "path"), ["a=b.yaml", ""]);
  assert.deepEqual(queryValues("", "path"), []);
});

test("a refused model link is one capped line with its control characters escaped", () => {
  const request = parseModelUri(modelLink(`path=a\u0007\n${"x".repeat(400)}.txt`), EXTENSION_ID);
  assert.equal(request.kind, "refused");
  if (request.kind !== "refused") {
    return;
  }
  const line = refusedModelLinkLine(request);
  assert.ok(line.startsWith("SWCraft: a model link was not opened: the name holds"));
  assert.equal(line.replace(CONTROL_CHARACTERS, ""), line, "one line, nothing raw");
  assert.ok(line.includes("a\\u0007\\u000a"));
  assert.ok(line.length < LINK_QUOTE_LIMIT + 120);
  assert.ok(line.includes("…"));
  assert.equal(
    refusedModelLinkLine({ reason: "the link names another extension", path: undefined }),
    "SWCraft: a model link was not opened: the link names another extension.",
  );
  assert.equal(channelQuote("\u2028\u0085ok"), "\\u2028\\u0085ok");
  assert.equal(channelQuote("abcdef", 3), "abc…");
  // Escaped first, then cut: control characters alone are bounded too, and no escape is split.
  const controls = channelQuote("\u0007".repeat(200));
  assert.ok(controls.length <= LINK_QUOTE_LIMIT + 1, `${controls.length}`);
  assert.ok(controls.endsWith("\\u0007…"), controls.slice(-12));
  assert.equal(
    channelQuote("ab\u0007", 5),
    "ab…",
    "a partial escape is dropped, never left broken",
  );
  assert.equal(channelQuote("ab\u0007", 3), "ab…");
});

test("uriRoute: the handler dispatches on the link's path first — /model is the model's, anything else the guide's", () => {
  assert.equal(uriRoute({ path: MODEL_URI_PATH }), "model");
  for (const other of ["/guide", "/model/", "/Model", "", "/"]) {
    assert.equal(uriRoute({ path: other }), "guide", other);
  }
});

/** A model directory with one file, one directory and one link to a file outside it. */
function modelLinkFixture(t: { after(fn: () => void): void }) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-model-link-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const model = path.join(dir, "model");
  fs.mkdirSync(path.join(model, "swc", "chassis"), { recursive: true });
  fs.writeFileSync(path.join(model, "swc", "chassis", "Sensor.yaml"), "name: Sensor\n", {
    encoding: "utf8",
  });
  fs.mkdirSync(path.join(model, "dir.yaml"));
  fs.writeFileSync(path.join(dir, "outside.yaml"), "name: Outside\n", { encoding: "utf8" });
  let linked = true;
  try {
    fs.symlinkSync(path.join(dir, "outside.yaml"), path.join(model, "link.yaml"));
  } catch {
    linked = false;
  }
  return { model, linked };
}

test("modelLinkAction: a valid link opens the real path of the project's file, with its storage name", (t) => {
  const { model } = modelLinkFixture(t);
  const action = modelLinkAction(
    modelLink(decodedByVsCode("path=swc%2Fchassis%2FSensor.yaml")),
    EXTENSION_ID,
    model,
  );
  assert.deepEqual(action, {
    kind: "open",
    path: fs.realpathSync.native(path.join(model, "swc", "chassis", "Sensor.yaml")),
    name: "swc/chassis/Sensor.yaml",
  });
});

test("modelLinkAction: no project open is the one notification, and it quotes nothing from the link", () => {
  const action = modelLinkAction(
    modelLink(decodedByVsCode("path=swc%2Fchassis%2FSecret%E2%80%A8Name.yaml")),
    EXTENSION_ID,
    undefined,
  );
  assert.deepEqual(action, { kind: "notify", message: NO_PROJECT_FOR_MODEL_LINK });
  assert.equal(/Secret|swc|chassis/.test(NO_PROJECT_FOR_MODEL_LINK), false);
});

test("modelLinkAction: a refused link is one channel line, never a notification, with or without a project", (t) => {
  const { model } = modelLinkFixture(t);
  for (const modelRoot of [model, undefined]) {
    for (const query of [
      "path=..%2Fswcraft.yaml",
      "path=%2Fetc%2Fpasswd.yaml",
      "",
      "path=a.yaml&path=b.yaml",
    ]) {
      const action = modelLinkAction(modelLink(decodedByVsCode(query)), EXTENSION_ID, modelRoot);
      assert.equal(
        action.kind,
        "log",
        `${query} (${modelRoot === undefined ? "no project" : "a project"})`,
      );
    }
    assert.equal(
      modelLinkAction(modelLink("path=a.yaml", "other.extension"), EXTENSION_ID, modelRoot).kind,
      "log",
    );
  }
});

test("modelLinkAction: a file missing, a directory or a link out of the model is one line, capped and escaped", (t) => {
  const { model, linked } = modelLinkFixture(t);
  const names = ["swc/chassis/Gone.yaml", "dir.yaml", ...(linked ? ["link.yaml"] : [])];
  for (const name of names) {
    const action = modelLinkAction(modelLink(`path=${name}`), EXTENSION_ID, model);
    assert.equal(action.kind, "log", name);
    if (action.kind === "log") {
      assert.ok(action.line.includes(`("${name}")`), action.line);
      assert.equal(action.line.includes(model), false, "no absolute path in the line");
    }
  }
  const long = `${"a".repeat(LINK_QUOTE_LIMIT + 50)}\u2028.yaml`;
  const action = modelLinkAction(modelLink(`path=${long}`), EXTENSION_ID, model);
  assert.equal(action.kind, "log");
  if (action.kind === "log") {
    assert.equal(CONTROL_CHARACTERS.test(action.line), false, "control characters escaped");
    CONTROL_CHARACTERS.lastIndex = 0;
    assert.ok(action.line.includes("…"), "the quote is capped");
  }
});

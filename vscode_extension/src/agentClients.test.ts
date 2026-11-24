// Unit tests for the client table as *Onboard Project* reads it (`agentClients.ts`): the quick
// pick's entries, the files a chosen set writes, the companion rule and the conflict warnings,
// all read from the bundled `clients.json`, a byte copy of
// `packages/tools/src/swcraft_tools/templates/clients.json` (`packageVsix.test.ts`).

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import {
  CLIENT_TABLE_ASSET_NAME,
  chosenClients,
  clientFilesFor,
  clientInUse,
  clientLabelsFor,
  clientNamed,
  clientPickItems,
  clientServerFiles,
  companionApplies,
  conflictWarnings,
  loadClientTable,
  parseClientTable,
  preselectedClients,
} from "./agentClients";
import { CLIENT_FILE_ASSETS, planOnboarding } from "./onboarding";
import { functionBody, readSource, withoutCommentLines } from "./testSupport";

const FIXTURES_AGENTS_DIR = path.join(__dirname, "..", "fixtures", "agents");
const TABLE_TEXT = fs.readFileSync(path.join(FIXTURES_AGENTS_DIR, CLIENT_TABLE_ASSET_NAME), "utf8");
const TABLE = loadClientTable(FIXTURES_AGENTS_DIR);
const NAMES = TABLE.clients.map((client) => client.name);

test("the bundled table is read: four clients, Copilot the default", () => {
  assert.deepEqual(NAMES, ["copilot", "codex", "claude", "kiro"]);
  assert.deepEqual(TABLE.default, ["copilot"]);
  assert.equal(TABLE.copilotCompanionSettings.file, ".vscode/settings.json");
});

// `vscode.env.uriScheme` in each host: VS Code's own, and Kiro's (the `urlProtocol` of
// /Applications/Kiro.app/Contents/Resources/app/product.json).
const VSCODE_HOST = "vscode";
const KIRO_HOST = "kiro";

test("clientPickItems: in VS Code one entry per row by its label, only the table's default pre-picked", () => {
  assert.deepEqual(
    clientPickItems(TABLE, VSCODE_HOST).map((item) => item.name),
    ["copilot", "codex", "claude", "kiro"],
  );
  assert.deepEqual(
    clientPickItems(TABLE, VSCODE_HOST).find((item) => item.name === "kiro"),
    { label: "Kiro", name: "kiro", picked: false },
  );
  assert.deepEqual(
    clientPickItems(TABLE, VSCODE_HOST),
    TABLE.clients.map((client) => ({
      label: client.label,
      name: client.name,
      picked: client.name === "copilot",
    })),
  );
});

test("the bundled table: Kiro's row alone names a host, Kiro's uriScheme", () => {
  assert.deepEqual(
    Object.fromEntries(TABLE.clients.map((client) => [client.name, client.preselectInHosts])),
    { copilot: [], codex: [], claude: [], kiro: [KIRO_HOST] },
  );
});

test("preselectedClients: the row naming the host, else the table's default", () => {
  assert.deepEqual(preselectedClients(TABLE, KIRO_HOST), ["kiro"]);
  assert.deepEqual(preselectedClients(TABLE, VSCODE_HOST), ["copilot"]);
  assert.deepEqual(preselectedClients(TABLE, "vscode-insiders"), ["copilot"]);
  assert.deepEqual(preselectedClients(TABLE, "Kiro"), ["copilot"]);
  assert.deepEqual(preselectedClients(TABLE, ""), ["copilot"]);
});

test("clientPickItems: in Kiro only Kiro is pre-picked", () => {
  assert.deepEqual(
    clientPickItems(TABLE, KIRO_HOST),
    TABLE.clients.map((client) => ({
      label: client.label,
      name: client.name,
      picked: client.name === "kiro",
    })),
  );
});

test("chosenClients: a cancelled pick and an empty one choose nothing; otherwise the picked names", () => {
  assert.equal(chosenClients(undefined), undefined);
  assert.equal(chosenClients([]), undefined);
  assert.deepEqual(chosenClients([{ name: "copilot" }, { name: "claude" }]), ["copilot", "claude"]);
});

test("clientFilesFor: each client's own files, a shared AGENTS.md once, in the table's order", () => {
  for (const client of TABLE.clients) {
    assert.deepEqual(clientFilesFor(TABLE, [client.name]), client.files, client.name);
  }
  assert.deepEqual(clientFilesFor(TABLE, ["claude", "codex"]), [
    "AGENTS.md",
    ".codex/config.toml",
    ".codex/hooks.json",
    "CLAUDE.md",
    ".mcp.json",
    ".claude/settings.json",
  ]);
  assert.deepEqual(clientFilesFor(TABLE, ["kiro"]), [
    ".kiro/settings/mcp.json",
    ".kiro/steering/swcraft.md",
    ".kiro/hooks/swcraft-guard.json",
  ]);
  assert.throws(() => clientFilesFor(TABLE, ["cursor"]), /no chat client named cursor/);
});

test("clientLabelsFor: the chosen rows' labels in the table's order", () => {
  assert.deepEqual(clientLabelsFor(TABLE, ["claude", "copilot"]), [
    TABLE.clients[0].label,
    TABLE.clients[2].label,
  ]);
});

test("conflictWarnings: Copilot with Claude Code, and Kiro with Codex or Claude Code, warn with the table's lines; no other set does", () => {
  const warning = TABLE.conflicts[0].warning;
  assert.match(warning, /Copilot also reads \.mcp\.json/);
  const kiro = TABLE.conflicts[1].warning;
  assert.match(kiro, /Kiro also reads AGENTS\.md/);
  assert.equal(TABLE.conflicts[2].warning, kiro);
  assert.deepEqual(conflictWarnings(TABLE, ["copilot", "claude"]), [warning]);
  assert.deepEqual(conflictWarnings(TABLE, ["kiro", "codex"]), [kiro]);
  assert.deepEqual(conflictWarnings(TABLE, ["claude", "kiro"]), [kiro]);
  // Kiro with both: the shared line once, where it first appears.
  assert.deepEqual(conflictWarnings(TABLE, ["kiro", "codex", "claude"]), [kiro]);
  assert.deepEqual(conflictWarnings(TABLE, NAMES), [warning, kiro]);
  for (const chosen of [
    ["copilot"],
    ["copilot", "codex"],
    ["claude"],
    ["codex", "claude"],
    ["kiro"],
    ["copilot", "kiro"],
  ]) {
    assert.deepEqual(conflictWarnings(TABLE, chosen), [], chosen.join("+"));
  }
});

test("companionApplies: Copilot with Codex or Claude Code, never without Copilot", () => {
  assert.equal(companionApplies(TABLE, ["copilot", "codex"]), true);
  assert.equal(companionApplies(TABLE, ["copilot", "claude"]), true);
  assert.equal(companionApplies(TABLE, NAMES), true);
  for (const chosen of [
    ["copilot"],
    ["codex"],
    ["claude"],
    ["codex", "claude"],
    ["copilot", "kiro"],
  ]) {
    assert.equal(companionApplies(TABLE, chosen), false, chosen.join("+"));
  }
});

test("every file a row names has an onboarding asset, and every asset is some row's file", () => {
  const tableFiles = new Set(TABLE.clients.flatMap((client) => client.files));
  assert.deepEqual([...CLIENT_FILE_ASSETS.keys()].sort(), [...tableFiles].sort());
  for (const [, asset] of CLIENT_FILE_ASSETS) {
    assert.ok(
      fs.statSync(path.join(FIXTURES_AGENTS_DIR, asset.assetName)).isFile(),
      asset.assetName,
    );
  }
  assert.throws(
    () => planOnboarding("/p", ["kiro.json"]),
    /kiro\.json, which has no onboarding asset/,
  );
});

test("parseClientTable: a damaged table throws, naming what is wrong", () => {
  const table = JSON.parse(TABLE_TEXT);
  const damaged = (change: (copy: Record<string, unknown>) => void): string => {
    const copy = structuredClone(table);
    change(copy);
    return JSON.stringify(copy);
  };
  assert.throws(() => parseClientTable("not json"), /is not JSON/);
  assert.throws(() => parseClientTable("[]"), /is not an object/);
  assert.throws(
    () => parseClientTable(damaged((copy) => delete copy.clients)),
    /must hold the keys/,
  );
  assert.throws(
    () =>
      parseClientTable(
        damaged((copy) => {
          (copy.clients as Record<string, unknown>[])[1].label = "";
        }),
      ),
    /row 1 label must be a non-empty string/,
  );
  assert.throws(
    () =>
      parseClientTable(
        damaged((copy) => {
          (copy.clients as Record<string, unknown>[])[1].name = "copilot";
        }),
      ),
    /repeated/,
  );
  assert.throws(
    () =>
      parseClientTable(
        damaged((copy) => {
          copy.default = ["cursor"];
        }),
      ),
    /the default names cursor/,
  );
  assert.throws(
    () =>
      parseClientTable(
        damaged((copy) => {
          (copy.conflicts as Record<string, unknown>[])[0].clients = ["copilot", "cursor"];
        }),
      ),
    /conflict 0 names cursor/,
  );
  assert.throws(
    () =>
      parseClientTable(
        damaged((copy) => {
          (copy.copilotCompanionSettings as Record<string, unknown>).anyOf = ["cursor"];
        }),
      ),
    /companion settings rule names cursor/,
  );
});

test("parseClientTable refuses what swcraft_tools.agent_clients refuses (test_agent_clients.py's malformed tables)", () => {
  const table = JSON.parse(TABLE_TEXT);
  const refused = (change: (copy: typeof table) => void, pattern: RegExp): void => {
    const copy = structuredClone(table);
    change(copy);
    assert.throws(() => parseClientTable(JSON.stringify(copy)), pattern);
  };
  refused((d) => delete d.default, /must hold the keys/);
  refused((d) => {
    d.extra = 1;
  }, /must hold the keys/);
  refused((d) => delete d.conflicts, /must hold the keys/);
  refused((d) => delete d.clients[0].label, /row 0 must hold exactly the keys/);
  refused((d) => {
    d.clients[0].extra = 1;
  }, /row 0 must hold exactly the keys/);
  refused((d) => d.clients.push(structuredClone(d.clients[0])), /repeated/);
  refused((d) => {
    d.clients[1].hook = "other.json";
  }, /row 1 hook must be null or one of its files/);
  refused((d) => {
    d.clients[1].server = 7;
  }, /row 1 server must be null or one of its files/);
  refused((d) => d.clients[1].files.push("../x"), /template-relative POSIX path/);
  refused((d) => d.clients[1].files.push("/abs"), /template-relative POSIX path/);
  refused((d) => d.clients[1].files.push("a\\b"), /template-relative POSIX path/);
  refused((d) => d.clients[1].files.push(d.clients[1].files[0]), /names an entry twice/);
  refused((d) => {
    d.clients[1].guideSection = "vscode.md";
  }, /<page>\.md#<anchor>/);
  refused((d) => {
    d.default = ["cursor"];
  }, /names cursor/);
  refused((d) => {
    d.conflicts[0].clients = ["copilot"];
  }, /at least two/);
  refused((d) => {
    d.conflicts[0].warning = "a\nb";
  }, /one line/);
  refused((d) => delete d.conflicts[0].guideSection, /conflict 0 must hold exactly the keys/);
  refused((d) => {
    d.copilotCompanionSettings.settings = {};
  }, /non-empty object/);
  refused((d) => {
    d.copilotCompanionSettings.extra = 1;
  }, /must hold exactly the keys/);
  refused((d) => {
    d.copilotCompanionSettings.file = "../settings.json";
  }, /template-relative POSIX path/);
  // The optional preselectInHosts column, as test_agent_clients.py's hosts-* cases.
  refused((d) => {
    d.clients[3].preselectInHosts = "kiro";
  }, /row 3 preselectInHosts must be a non-empty list/);
  refused((d) => {
    d.clients[3].preselectInHosts = [];
  }, /row 3 preselectInHosts must be a non-empty list/);
  refused((d) => {
    d.clients[3].preselectInHosts = [""];
  }, /row 3 preselectInHosts\[0\] must be a non-empty string/);
  refused((d) => {
    d.clients[3].preselectInHosts = [7];
  }, /row 3 preselectInHosts\[0\] must be a non-empty string/);
  refused((d) => {
    d.clients[3].preselectInHosts = ["kiro", "kiro"];
  }, /names an entry twice/);
  refused((d) => {
    d.clients[0].preselectInHosts = ["kiro"];
  }, /preselectInHosts names the host kiro in two rows/);
});

test("parseClientTable: preselectInHosts is optional", () => {
  const copy = JSON.parse(TABLE_TEXT);
  delete copy.clients[3].preselectInHosts;
  assert.deepEqual(
    clientNamed(parseClientTable(JSON.stringify(copy)), "kiro").preselectInHosts,
    [],
  );
});

test("clientServerFiles: each row's server by the row's label, in the table's order", () => {
  assert.deepEqual(
    clientServerFiles(TABLE),
    TABLE.clients
      .filter((client) => client.server !== null)
      .map((client) => ({ relativePath: client.server, client: client.label })),
  );
  assert.equal(clientServerFiles(TABLE).length, 3);
  assert.deepEqual(clientServerFiles(TABLE).at(-1), {
    relativePath: ".kiro/settings/mcp.json",
    client: "Kiro",
  });
});

test("clientInUse: a file of the row, or no other row's file; a shared file counts for neither", () => {
  const present =
    (...files: string[]) =>
    (relative: string) =>
      files.includes(relative);
  assert.ok(clientInUse(TABLE, "copilot", present()));
  assert.ok(clientInUse(TABLE, "copilot", present(".github/hooks/swcraft.json", ".mcp.json")));
  assert.ok(!clientInUse(TABLE, "copilot", present(".mcp.json")));
  assert.ok(!clientInUse(TABLE, "copilot", present("AGENTS.md")));
  // AGENTS.md is Codex's and Claude Code's both: for Codex it is no other client's file.
  assert.ok(clientInUse(TABLE, "codex", present("AGENTS.md")));
  assert.ok(!clientInUse(TABLE, "codex", present(".mcp.json")));
  assert.ok(clientInUse(TABLE, "kiro", present(".kiro/steering/swcraft.md")));
  assert.ok(!clientInUse(TABLE, "kiro", present("AGENTS.md")));
  assert.throws(() => clientInUse(TABLE, "cursor", present()), /no chat client named cursor/);
});

test("no extension module copies the table: its labels, warning and companion keys appear in no source", () => {
  // The quick pick, the files, the warning and the companion keys come from the bundled
  // clients.json at run time; a spelling of them in code would be a second table to keep in step.
  // A label is looked for as a string literal: Kiro's bare product name is also spelled as prose
  // in comments and in the guide page's title (`GUIDE_SECTION_TITLES`).
  const spellings = [
    ...TABLE.clients.flatMap((client) =>
      ['"', "'", "`"].map((quote) => `${quote}${client.label}${quote}`),
    ),
    // A client named in code by a literal.
    'client: "',
    ...TABLE.conflicts.map((conflict) => conflict.warning),
    ...Object.keys(TABLE.copilotCompanionSettings.settings),
  ];
  for (const module of [
    "src/agentClients.ts",
    "src/onboarding.ts",
    "src/commands.ts",
    "src/clientSwcraft.ts",
    "src/healthCheck.ts",
  ]) {
    const source = readSource(module);
    for (const spelling of spellings) {
      assert.equal(source.includes(spelling), false, `${module} spells ${spelling}`);
    }
  }
});

test("Onboard Project asks with a multi-select quick pick of the table's clients and stops on a cancel", () => {
  const body = withoutCommentLines(
    functionBody(readSource("src/commands.ts"), "async function onboardProjectCommand("),
  );
  assert.match(body, /table = loadClientTable\(onboardingAssetsDir\(context\)\)/);
  // A table that cannot be read has its own message: nothing was written yet.
  assert.match(body, /catch \(err\) \{\s*const message = clientTableUnreadableMessage\(err\);/);
  assert.match(
    body,
    /showQuickPick\(\s*clientPickItems\(table, vscode\.env\.uriScheme\),\s*\{\s*canPickMany: true,/,
  );
  assert.match(
    body,
    /const chosen = chosenClients\(pickedClients\);\s*if \(chosen === undefined\) \{/,
  );
  // The pick comes before anything spawns.
  assert.ok(body.indexOf("showQuickPick(") < body.indexOf("withRunIndicator("));
  assert.match(body, /runOnboarding\(targetDir, table, chosen,/);
});

test("the notice reads the table for its Copilot plan", () => {
  const body = functionBody(
    readSource("src/commands.ts"),
    "export async function offerAgentProfile(",
  );
  assert.match(
    body,
    /planAgentProfileNotice\(projectDir, loadClientTable\(onboardingAssetsDir\(context\)\)\)/,
  );
});

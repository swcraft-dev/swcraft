// Unit tests for the `swcraft`-on-PATH check of Codex's, Claude Code's and Kiro's server
// files. Every reading is injected, so no process is spawned and no project file is read (one
// test reads the bundled Kiro server asset, the file Onboard Project writes).

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";
import { clientServerFiles, loadClientTable } from "./agentClients";
import {
  type ClientServerFile,
  type ClientSwcraftInput,
  checkClientSwcraft,
  clientSwcraftTrigger,
  clientSwcraftWarning,
  namesBareSwcraft,
  presentClientServerFiles,
  sameFile,
} from "./clientSwcraft";
import type { VersionProbe } from "./healthCheck";
import {
  CLIENT_FILES_GUIDE_SECTION,
  KIRO_MCP_JSON_ASSET_NAME,
  TOML_MARKER_LINE,
} from "./onboarding";
import { functionBody, readSource, withoutCommentLines } from "./testSupport";
import { resolveBareCommand } from "./toolchain";

const OWN = "/home/u/.swcraft/current/bin/swcraft";
const OLD = "/home/u/old/bin/swcraft";
const TABLE = loadClientTable(path.join(__dirname, "..", "fixtures", "agents"));
const CODEX_LABEL = TABLE.clients.find((client) => client.name === "codex")?.label ?? "";
const CLAUDE_LABEL = TABLE.clients.find((client) => client.name === "claude")?.label ?? "";
const KIRO_LABEL = TABLE.clients.find((client) => client.name === "kiro")?.label ?? "";
/** Codex's and Claude Code's server files, the pair most tests name. */
const BOTH: readonly ClientServerFile[] = clientServerFiles(TABLE).filter(
  (file) => file.client !== KIRO_LABEL,
);

/** *text* as a regular expression matching it literally. */
function literally(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function versions(table: Record<string, VersionProbe>) {
  const asked: string[] = [];
  const probe = async (executable: string): Promise<VersionProbe> => {
    asked.push(executable);
    return table[executable] ?? { ok: false, output: "" };
  };
  return { probe, asked };
}

function input(overrides: Partial<ClientSwcraftInput>): ClientSwcraftInput {
  return {
    clientFiles: BOTH,
    own: { executable: OWN, source: "installation", exists: true },
    onPath: OWN,
    platform: "posix",
    realpath: (candidate) => candidate,
    probe: async () => ({ ok: true, output: "swcraft 0.23.0\n" }),
    ...overrides,
  };
}

async function warningFor(overrides: Partial<ClientSwcraftInput>): Promise<string | undefined> {
  const full = input(overrides);
  return clientSwcraftWarning(await checkClientSwcraft(full), full.clientFiles);
}

// --- which files trigger it ---------------------------------------------------

test("the table's server files trigger it, by their rows' labels, and the hook files do not", () => {
  assert.deepEqual(clientServerFiles(TABLE), [
    { relativePath: ".codex/config.toml", client: CODEX_LABEL },
    { relativePath: ".mcp.json", client: CLAUDE_LABEL },
    { relativePath: ".kiro/settings/mcp.json", client: KIRO_LABEL },
  ]);
  const seen: string[] = [];
  const mcpJson = path.join("/p", ".mcp.json");
  const found = presentClientServerFiles("/p", TABLE, (candidate) => {
    seen.push(candidate);
    return candidate === mcpJson ? '{"mcpServers":{"swcraft":{"command":"swcraft"}}}' : undefined;
  });
  assert.deepEqual(
    found.map((file) => file.client),
    [CLAUDE_LABEL],
  );
  assert.deepEqual(seen, [
    path.join("/p", ".codex", "config.toml"),
    mcpJson,
    path.join("/p", ".kiro", "settings", "mcp.json"),
  ]);
});

test("a server file triggers it only when it names a bare swcraft", () => {
  const filesFor = (texts: Record<string, string>) =>
    presentClientServerFiles("/p", TABLE, (candidate) => {
      const relative = path.relative("/p", candidate).split(path.sep).join("/");
      return texts[relative];
    }).map((file) => file.relativePath);
  // A user's own .mcp.json without a swcraft server draws no warning; one with it does.
  assert.deepEqual(
    filesFor({ ".mcp.json": '{"mcpServers":{"mine":{"command":"my-server"}}}' }),
    [],
  );
  assert.deepEqual(
    filesFor({
      ".mcp.json": '{"mcpServers":{"mine":{"command":"x"},"tools":{"command":"swcraft"}}}',
    }),
    [".mcp.json"],
  );
  assert.deepEqual(
    filesFor({ ".mcp.json": '{"mcpServers":{"s":{"command":"/abs/swcraft"}}}' }),
    [],
  );
  assert.deepEqual(filesFor({ ".mcp.json": "not json" }), []);
  // .codex/config.toml: SWCraft's marker line, or a `command = "swcraft"` line.
  assert.deepEqual(filesFor({ ".codex/config.toml": `${TOML_MARKER_LINE}\n[x]\n` }), [
    ".codex/config.toml",
  ]);
  for (const line of ['command = "swcraft"', "  command='swcraft'  ", 'command="swcraft"']) {
    assert.deepEqual(filesFor({ ".codex/config.toml": `[mcp_servers.s]\n${line}\n` }), [
      ".codex/config.toml",
    ]);
  }
  assert.deepEqual(filesFor({ ".codex/config.toml": '[mcp_servers.s]\ncommand = "other"\n' }), []);
  assert.equal(namesBareSwcraft(".codex/config.toml", 'command = "swcraft-x"'), false);
  // .kiro/settings/mcp.json is read as JSON, as .mcp.json is.
  const kiro = ".kiro/settings/mcp.json";
  assert.deepEqual(
    filesFor({ [kiro]: '{"mcpServers":{"swcraft":{"command":"swcraft","timeout":1}}}' }),
    [kiro],
  );
  assert.deepEqual(filesFor({ [kiro]: '{"mcpServers":{"s":{"command":"/abs/swcraft"}}}' }), []);
  assert.deepEqual(filesFor({ [kiro]: `${TOML_MARKER_LINE}\ncommand = "swcraft"\n` }), []);
});

test("Kiro's server file: the bundled one triggers it, and the warning names Kiro and the file", async () => {
  const kiroMcp = fs.readFileSync(
    path.join(__dirname, "..", "fixtures", "agents", KIRO_MCP_JSON_ASSET_NAME),
    "utf8",
  );
  const found = presentClientServerFiles("/p", TABLE, (candidate) =>
    candidate === path.join("/p", ".kiro", "settings", "mcp.json") ? kiroMcp : undefined,
  );
  assert.deepEqual(found, [{ relativePath: ".kiro/settings/mcp.json", client: KIRO_LABEL }]);
  const warning = await warningFor({ clientFiles: found, onPath: undefined });
  assert.ok(warning !== undefined);
  assert.match(warning, new RegExp(`^SWCraft: ${literally(KIRO_LABEL)} in this window`));
  assert.match(
    warning,
    /\.kiro\/settings\/mcp\.json starts the SWCraft server as a bare `swcraft`/,
  );
});

test("a workspace without client files is not checked: no probe, no warning", async () => {
  const { probe, asked } = versions({});
  assert.equal(await warningFor({ clientFiles: [], onPath: undefined, probe }), undefined);
  assert.deepEqual(asked, []);
});

test("SWCraft's own executable absent: no warning — Check Editor Tooling's first finding", async () => {
  const { probe, asked } = versions({});
  const warning = await warningFor({
    own: { executable: OWN, source: "installation", exists: false },
    onPath: undefined,
    probe,
  });
  assert.equal(warning, undefined);
  assert.deepEqual(asked, []);
});

// --- the findings -------------------------------------------------------------

test("no swcraft on PATH: one warning naming SWCraft's executable and version, and the guide", async () => {
  const { probe, asked } = versions({ [OWN]: { ok: true, output: "swcraft 0.23.0\n" } });
  const warning = await warningFor({ onPath: undefined, probe });
  assert.deepEqual(asked, [OWN]);
  assert.ok(warning !== undefined);
  assert.match(
    warning,
    new RegExp(
      `^SWCraft: ${literally(CODEX_LABEL)} and ${literally(CLAUDE_LABEL)} in this window will find no \`swcraft\`\\.`,
    ),
  );
  assert.match(warning, /\.codex\/config\.toml and \.mcp\.json start the SWCraft server/);
  assert.match(
    warning,
    /SWCraft uses \/home\/u\/\.swcraft\/current\/bin\/swcraft \(version 0\.23\.0\)/,
  );
  assert.match(warning, /can show version 0\.23\.0 while the PATH the chats inherit/);
  assert.ok(warning.includes(`"${CLIENT_FILES_GUIDE_SECTION}" section (In VS Code)`));
  assert.match(warning, /from the Dock or Finder/);
  assert.match(warning, /The check is that this warning does not come back;/);
  assert.match(warning, /on Windows and Linux it is the same check, .*\(tried on macOS only\)\./);
  assert.match(warning, /in VS Code's own terminal is only a hint: that terminal's shell re-reads/);
});

test("the same path: no warning, and nothing is probed", async () => {
  const { probe, asked } = versions({});
  assert.equal(await warningFor({ onPath: OWN, probe }), undefined);
  assert.deepEqual(asked, []);
});

test("a link to the same file: no warning (compared after resolving links)", async () => {
  const link = "/usr/local/bin/swcraft";
  const realpath = (candidate: string) =>
    candidate === link || candidate === OWN
      ? "/home/u/.swcraft/versions/0.23.0/bin/swcraft"
      : candidate;
  assert.equal(await warningFor({ onPath: link, realpath }), undefined);
});

test("a different path and an older version: the warning names both, and the pin refusal", async () => {
  const { probe, asked } = versions({
    [OWN]: { ok: true, output: "swcraft 0.23.0\n" },
    [OLD]: { ok: true, output: "swcraft 0.19.2\n" },
  });
  const warning = await warningFor({ onPath: OLD, probe });
  assert.deepEqual([...asked].sort(), [OLD, OWN].sort());
  assert.ok(warning !== undefined);
  assert.match(warning, /would start a different `swcraft` from SWCraft's/);
  assert.match(warning, /resolves it to \/home\/u\/old\/bin\/swcraft \(version 0\.19\.2\)/);
  assert.match(
    warning,
    /SWCraft uses \/home\/u\/\.swcraft\/current\/bin\/swcraft \(version 0\.23\.0\)/,
  );
  assert.match(warning, /refuses the project's toolchain pin/);
});

test("a different path with the same version: still a warning — another installation (decided)", async () => {
  const { probe } = versions({
    [OWN]: { ok: true, output: "swcraft 0.23.0\n" },
    [OLD]: { ok: true, output: "swcraft 0.23.0\n" },
  });
  const warning = await warningFor({ onPath: OLD, probe });
  assert.ok(warning !== undefined);
  assert.match(warning, /The versions agree, but it is another installation/);
  assert.doesNotMatch(warning, /toolchain pin/);
});

test("a version probe that fails is named in the warning", async () => {
  const { probe } = versions({
    [OWN]: { ok: true, output: "swcraft 0.23.0\n" },
    [OLD]: { ok: false, output: "Traceback (most recent call last):\n  ..." },
  });
  const warning = await warningFor({ onPath: OLD, probe });
  assert.ok(warning !== undefined);
  assert.match(
    warning,
    /\/home\/u\/old\/bin\/swcraft, which did not answer `swcraft --version` \(Traceback \(most recent call last\):\)/,
  );
});

test("SWCraft's own probe failing: named, and the remedy names no release it cannot read", async () => {
  const { probe } = versions({ [OWN]: { ok: false, output: "" } });
  const warning = await warningFor({ onPath: undefined, probe });
  assert.ok(warning !== undefined);
  assert.match(warning, /which did not answer `swcraft --version` \(no output\)/);
  assert.match(warning, /can show the release SWCraft uses/);
});

test("one client file: the warning names that client and file alone", async () => {
  const codex = BOTH.filter((file) => file.client === CODEX_LABEL);
  const warning = await warningFor({ clientFiles: codex, onPath: undefined });
  assert.ok(warning !== undefined);
  assert.match(warning, new RegExp(`^SWCraft: ${literally(CODEX_LABEL)} in this window`));
  assert.match(warning, /\.codex\/config\.toml starts the SWCraft server/);
  // "Claude Code" still appears once — in the guide section's own title.
  assert.doesNotMatch(warning, /\.mcp\.json/);
  assert.ok(!warning.includes(CLAUDE_LABEL));
});

test("sameFile: case-insensitive on Windows, by case on POSIX, as given when realpath throws", () => {
  const throws = () => {
    throw new Error("ENOENT");
  };
  assert.ok(sameFile("C:\\A\\swcraft.exe", "c:/a/SWCRAFT.EXE", "windows", throws));
  assert.ok(!sameFile("/a/swcraft", "/A/swcraft", "posix", throws));
});

// --- one notification for several triggers ------------------------------------

test("several triggers show exactly one warning", async () => {
  const shown: string[] = [];
  let reads = 0;
  const trigger = clientSwcraftTrigger(
    async () => {
      reads += 1;
      return "the warning";
    },
    (message) => shown.push(message),
  );
  await Promise.all([trigger("/p"), trigger("/p"), trigger("/p")]);
  await trigger("/p");
  assert.deepEqual(shown, ["the warning"]);
  // The first trigger reads and shows; the two arriving while it runs only queue a
  // re-run, which the shown warning cancels; the later one reads nothing.
  assert.equal(reads, 1);
});

test("a trigger during a running check runs once more after it, with the latest folder", async () => {
  const shown: string[] = [];
  const read: string[] = [];
  let release: () => void = () => undefined;
  const firstRead = new Promise<void>((resolve) => {
    release = resolve;
  });
  const trigger = clientSwcraftTrigger(
    async (projectDir) => {
      read.push(projectDir);
      if (read.length === 1) {
        await firstRead;
        return undefined; // activation: no client files yet
      }
      return `warning for ${projectDir}`;
    },
    (message) => shown.push(message),
  );
  const activation = trigger("/window");
  const onboarding = trigger("/onboarded");
  release();
  await Promise.all([activation, onboarding]);
  assert.deepEqual(read, ["/window", "/onboarded"]);
  assert.deepEqual(shown, ["warning for /onboarded"]);
});

test("no warning to show: every trigger reads again, none shows anything", async () => {
  const shown: string[] = [];
  let reads = 0;
  const trigger = clientSwcraftTrigger(
    async () => {
      reads += 1;
      return undefined;
    },
    (message) => shown.push(message),
  );
  await trigger("/p");
  await trigger("/p");
  assert.equal(reads, 2);
  assert.deepEqual(shown, []);
});

// --- resolveBareCommand ------------------------------------------------------

test("resolveBareCommand: POSIX — the bare name, first executable in PATH order", () => {
  const executable = new Set(["/b/swcraft", "/c/swcraft"]);
  assert.equal(
    resolveBareCommand("swcraft", { PATH: "/a:/b:/c" }, "posix", (c) => executable.has(c)),
    "/b/swcraft",
  );
  assert.equal(
    resolveBareCommand("swcraft", { PATH: "/a" }, "posix", (c) => executable.has(c)),
    undefined,
  );
});

test("resolveBareCommand: Windows honours %PATHEXT% in its order, per directory", () => {
  const files = new Set(["C:\\a\\swcraft.BAT", "C:\\b\\swcraft.EXE"]);
  const isExecutable = (candidate: string) => files.has(candidate);
  assert.equal(
    resolveBareCommand(
      "swcraft",
      { PATH: "C:\\b;C:\\a", PATHEXT: ".COM;.EXE;.BAT" },
      "windows",
      isExecutable,
    ),
    "C:\\b\\swcraft.EXE",
  );
  // A directory earlier on PATH wins over an extension earlier in PATHEXT.
  assert.equal(
    resolveBareCommand(
      "swcraft",
      { PATH: "C:\\a;C:\\b", PATHEXT: ".COM;.EXE;.BAT" },
      "windows",
      isExecutable,
    ),
    "C:\\a\\swcraft.BAT",
  );
  // An extension PATHEXT does not list is not found.
  assert.equal(
    resolveBareCommand("swcraft", { PATH: "C:\\a", PATHEXT: ".EXE" }, "windows", isExecutable),
    undefined,
  );
});

test("resolveBareCommand: a blank %PATHEXT% reads the default list; the name is read in any case", () => {
  const lookups = (env: Record<string, string>) => {
    const seen: string[] = [];
    resolveBareCommand("swcraft", { PATH: "C:\\a", ...env }, "windows", (candidate) => {
      seen.push(candidate);
      return false;
    });
    return seen;
  };
  const defaults = lookups({});
  assert.deepEqual(lookups({ PATHEXT: "  " }), defaults);
  assert.deepEqual(lookups({ Pathext: ".EXE" }), ["C:\\a\\swcraft.EXE"]);
  assert.deepEqual(lookups({ pathext: ".CMD" }), ["C:\\a\\swcraft.CMD"]);
});

test("resolveBareCommand: Windows without %PATHEXT% reads the default list", () => {
  const seen: string[] = [];
  resolveBareCommand("swcraft", { PATH: "C:\\a" }, "windows", (candidate) => {
    seen.push(candidate);
    return false;
  });
  assert.deepEqual(seen, [
    "C:\\a\\swcraft.COM",
    "C:\\a\\swcraft.EXE",
    "C:\\a\\swcraft.BAT",
    "C:\\a\\swcraft.CMD",
  ]);
});

// --- the wiring ----------------------------------------------------------------

test("the adapter runs the check on activation and after Onboard Project, never on a save", () => {
  const extension = withoutCommentLines(readSource("src/extension.ts"));
  assert.match(extension, /checkClientSwcraftOnPath\(context\)\.catch\(logClientSwcraftFailure\)/);
  const commands = withoutCommentLines(readSource("src/commands.ts"));
  const onboarding = functionBody(commands, "async function runOnboarding(");
  assert.match(
    onboarding,
    /checkClientSwcraftOnPath\(context, targetDir\)\.catch\(logClientSwcraftFailure\)/,
  );
  const callers = [...commands.matchAll(/checkClientSwcraftOnPath\(/g)].length;
  // The definition and the onboarding call; nothing on a save or a watcher.
  assert.equal(callers, 2, `commands.ts calls checkClientSwcraftOnPath ${callers - 1} times`);
  assert.match(
    commands,
    /resolveBareCommand\(SWCRAFT_COMMAND_NAME, process\.env, platform, isExecutableFile\)/,
  );
});

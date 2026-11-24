// Pure logic: the chat clients *Onboard Project* offers, read from the one declared client
// table.
//
// The table is `packages/tools/src/swcraft_tools/templates/clients.json`; the extension
// reads a byte-equal copy among its onboarding assets (`fixtures/agents/clients.json`,
// shipped as `dist/onboarding-assets/clients.json`), pinned by `packageVsix.test.ts` and
// `packages/mcp/tests/test_vscode_agent_profile.py`. Everything is read from the table at
// run time, so a further client is a row of the table plus its assets (`onboarding.ts`'s
// `CLIENT_FILE_ASSETS`). No `vscode` import: the quick pick is `commands.ts`'s.
//
// The reader refuses what `swcraft_tools.agent_clients` refuses, so the two readers cannot
// accept different tables. What the checks here do not reach: whether a listed file exists
// in the template, whether a guide anchor exists (`packages/tools/tests/test_agent_clients.py`),
// and whether every file has an onboarding asset (`onboarding.ts`'s tests).

import * as fs from "node:fs";
import * as path from "node:path";

/** The table's file name among the onboarding assets. */
export const CLIENT_TABLE_ASSET_NAME = "clients.json";

/** One row of the table. */
export interface AgentClient {
  /** The CLI's `--client` value. */
  name: string;
  /** The quick pick's label. */
  label: string;
  /** Its files, POSIX paths relative to the template (and so to the project). */
  files: string[];
  hook: string | null;
  server: string | null;
  /** `<page>.md#<anchor>` in the user guide. */
  guideSection: string;
  /**
   * The editor hosts (`vscode.env.uriScheme`) in which the quick pick pre-picks this row
   * instead of the table's `default`; empty when the row names none.
   */
  preselectInHosts: string[];
}

/** A combination that works but clashes: its warning is shown when every one of `clients` is chosen. */
export interface ClientConflict {
  clients: string[];
  warning: string;
  guideSection: string;
}

/** The combination rule: `settings` into `file` when `with` is chosen together with any of `anyOf`. */
export interface CompanionSettings {
  file: string;
  with: string;
  anyOf: string[];
  settings: Record<string, unknown>;
}

/** The whole table, as the extension reads it. */
export interface ClientTable {
  template: string;
  default: string[];
  clients: AgentClient[];
  conflicts: ClientConflict[];
  copilotCompanionSettings: CompanionSettings;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function fail(what: string): never {
  throw new Error(`the client table (${CLIENT_TABLE_ASSET_NAME}) is malformed: ${what}`);
}

/** The keys `swcraft_tools.agent_clients` requires, each set exactly (the table also allows `$comment`). */
const TABLE_KEYS = ["template", "default", "clients", "conflicts", "copilotCompanionSettings"];
const ROW_KEYS = ["name", "label", "files", "hook", "server", "guideSection"];
const OPTIONAL_ROW_KEYS = ["preselectInHosts"];
const CONFLICT_KEYS = ["clients", "warning", "guideSection"];
const COMPANION_KEYS = ["file", "with", "anyOf", "settings"];

/** Whether *value* holds exactly *keys* (*optional* allowed beside them). */
function hasExactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
  optional: readonly string[] = [],
): boolean {
  const present = Object.keys(value);
  return (
    keys.every((key) => present.includes(key)) &&
    present.every((key) => keys.includes(key) || optional.includes(key))
  );
}

function nonEmptyString(value: unknown, where: string): string {
  if (typeof value !== "string" || value === "") {
    fail(`${where} must be a non-empty string`);
  }
  return value;
}

/** A non-empty list of non-empty strings, none twice. */
function stringList(value: unknown, where: string): string[] {
  if (!Array.isArray(value) || value.length === 0) {
    fail(`${where} must be a non-empty list`);
  }
  const items = value.map((item, index) => nonEmptyString(item, `${where}[${index}]`));
  if (new Set(items).size !== items.length) {
    fail(`${where} names an entry twice`);
  }
  return items;
}

/** A template-relative POSIX path: not absolute, no backslash, no empty, `.` or `..` part. */
function relativePath(value: string, where: string): string {
  if (
    value.startsWith("/") ||
    value.includes("\\") ||
    value.split("/").some((part) => part === "" || part === "." || part === "..")
  ) {
    fail(`${where} must be a template-relative POSIX path: ${value}`);
  }
  return value;
}

/** `<page>.md#<anchor>`. */
function guideSection(value: unknown, where: string): string {
  const section = nonEmptyString(value, where);
  const hash = section.indexOf("#");
  if (hash === -1 || !section.slice(0, hash).endsWith(".md") || hash === section.length - 1) {
    fail(`${where} must be <page>.md#<anchor>`);
  }
  return section;
}

/** `null`, or one of the row's *files*. */
function memberOrNull(value: unknown, files: readonly string[], where: string): string | null {
  if (value === null) {
    return null;
  }
  if (typeof value !== "string" || !files.includes(value)) {
    fail(`${where} must be null or one of its files`);
  }
  return value;
}

function parseRow(row: unknown, index: number): AgentClient {
  const where = `row ${index}`;
  if (!isPlainObject(row) || !hasExactKeys(row, ROW_KEYS, OPTIONAL_ROW_KEYS)) {
    fail(
      `${where} must hold exactly the keys ${ROW_KEYS.join(", ")} (and optionally ${OPTIONAL_ROW_KEYS.join(", ")})`,
    );
  }
  const files = stringList(row.files, `${where} files`).map((file) =>
    relativePath(file, `${where} files`),
  );
  return {
    name: nonEmptyString(row.name, `${where} name`),
    label: nonEmptyString(row.label, `${where} label`),
    files,
    hook: memberOrNull(row.hook, files, `${where} hook`),
    server: memberOrNull(row.server, files, `${where} server`),
    guideSection: guideSection(row.guideSection, `${where} guide section`),
    preselectInHosts:
      "preselectInHosts" in row
        ? stringList(row.preselectInHosts, `${where} preselectInHosts`)
        : [],
  };
}

function parseConflict(row: unknown, index: number): ClientConflict {
  const where = `conflict ${index}`;
  if (!isPlainObject(row) || !hasExactKeys(row, CONFLICT_KEYS)) {
    fail(`${where} must hold exactly the keys ${CONFLICT_KEYS.join(", ")}`);
  }
  const clients = stringList(row.clients, `${where} clients`);
  if (clients.length < 2) {
    fail(`${where} must name at least two clients`);
  }
  const warning = nonEmptyString(row.warning, `${where} warning`);
  if (warning.includes("\n")) {
    fail(`${where} warning must be one line`);
  }
  return {
    clients,
    warning,
    guideSection: guideSection(row.guideSection, `${where} guide section`),
  };
}

/**
 * *text* read as the client table, checked as `swcraft_tools.agent_clients` checks it:
 * exact keys; non-empty strings and lists with no entry twice; template-relative POSIX
 * paths; a hook or server `null` or one of the row's files; a guide section
 * `<page>.md#<anchor>`; no host in two rows' `preselectInHosts`; a conflict of two clients
 * or more with a one-line warning; non-empty companion settings; every name the default, a
 * conflict or the companion rule gives a client of the table. Anything else throws, naming
 * what is wrong, so a damaged copy fails loudly instead of onboarding with half a table.
 */
export function parseClientTable(text: string): ClientTable {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    fail("it is not JSON");
  }
  if (!isPlainObject(parsed)) {
    fail("it is not an object");
  }
  if (!hasExactKeys(parsed, TABLE_KEYS, ["$comment"])) {
    fail(`it must hold the keys ${TABLE_KEYS.join(", ")}`);
  }
  if (!Array.isArray(parsed.clients) || parsed.clients.length === 0) {
    fail("its clients must be a non-empty list");
  }
  const clients = parsed.clients.map(parseRow);
  const names = new Set(clients.map((client) => client.name));
  if (names.size !== clients.length) {
    fail("its client names are repeated");
  }
  const hosts = clients.flatMap((client) => client.preselectInHosts);
  for (const host of hosts) {
    if (hosts.indexOf(host) !== hosts.lastIndexOf(host)) {
      fail(`preselectInHosts names the host ${host} in two rows`);
    }
  }
  const known = (list: string[], where: string): string[] => {
    for (const name of list) {
      if (!names.has(name)) {
        fail(`${where} names ${name}, which is no client of the table`);
      }
    }
    return list;
  };
  if (!Array.isArray(parsed.conflicts)) {
    fail("its conflicts are not a list");
  }
  const conflicts = parsed.conflicts.map(parseConflict);
  conflicts.forEach((conflict, index) => {
    known(conflict.clients, `conflict ${index}`);
  });
  const companion = parsed.copilotCompanionSettings;
  if (!isPlainObject(companion) || !hasExactKeys(companion, COMPANION_KEYS)) {
    fail(`its companion settings rule must hold exactly the keys ${COMPANION_KEYS.join(", ")}`);
  }
  if (!isPlainObject(companion.settings) || Object.keys(companion.settings).length === 0) {
    fail("its companion settings must be a non-empty object");
  }
  const where = "the companion settings rule";
  return {
    template: nonEmptyString(parsed.template, "its template"),
    default: known(stringList(parsed.default, "its default"), "the default"),
    clients,
    conflicts,
    copilotCompanionSettings: {
      file: relativePath(nonEmptyString(companion.file, `${where} file`), `${where} file`),
      with: known([nonEmptyString(companion.with, `${where} client`)], where)[0],
      anyOf: known(stringList(companion.anyOf, `${where} pairs`), where),
      settings: companion.settings,
    },
  };
}

/** The table shipped in *assetsDir* (`dist/onboarding-assets/`, or `fixtures/agents/` under `node --test`). */
export function loadClientTable(assetsDir: string): ClientTable {
  return parseClientTable(fs.readFileSync(path.join(assetsDir, CLIENT_TABLE_ASSET_NAME), "utf8"));
}

/** The row named *name*; an unknown name throws. */
export function clientNamed(table: ClientTable, name: string): AgentClient {
  const client = table.clients.find((row) => row.name === name);
  if (client === undefined) {
    throw new Error(`no chat client named ${name} in the client table`);
  }
  return client;
}

/**
 * The files the *chosen* clients write, POSIX paths relative to the project, in
 * the table's row order, a file two clients share (`AGENTS.md`) once.
 */
export function clientFilesFor(table: ClientTable, chosen: readonly string[]): string[] {
  for (const name of chosen) {
    clientNamed(table, name);
  }
  const files: string[] = [];
  for (const client of table.clients) {
    if (!chosen.includes(client.name)) {
      continue;
    }
    for (const file of client.files) {
      if (!files.includes(file)) {
        files.push(file);
      }
    }
  }
  return files;
}

/** A client server file that names the SWCraft server, and the label of the client that reads it. */
export interface ClientServerFile {
  /** Relative to the project folder, `/`-separated, as the table spells it. */
  relativePath: string;
  /** The row's `label`. */
  client: string;
}

/** The table's server files — each row's `server`, where it has one — in the table's row order. */
export function clientServerFiles(table: ClientTable): ClientServerFile[] {
  return table.clients.flatMap((client) =>
    client.server === null ? [] : [{ relativePath: client.server, client: client.label }],
  );
}

/**
 * Whether the project whose files *exists* answers uses the client *name*: some file of its
 * row is present, or no file of any other row is (a project set up by `swcraft init`
 * without `--client` has Copilot's files or none). A shared file (`AGENTS.md`) counts for
 * neither side of the second test. What this does not reach: a project whose user removed
 * every file of each row, and a user's own file at another row's path, which reads as that
 * client in use.
 */
export function clientInUse(
  table: ClientTable,
  name: string,
  exists: (relativePath: string) => boolean,
): boolean {
  const own = clientNamed(table, name).files;
  if (own.some(exists)) {
    return true;
  }
  return !table.clients
    .filter((client) => client.name !== name)
    .some((client) => client.files.some((file) => !own.includes(file) && exists(file)));
}

/** The labels of the *chosen* clients, in the table's row order. */
export function clientLabelsFor(table: ClientTable, chosen: readonly string[]): string[] {
  return table.clients.filter((client) => chosen.includes(client.name)).map((row) => row.label);
}

/**
 * The warning of every conflict the *chosen* set meets, in the table's order, each line
 * once (two entries may carry one line), as `swcraft_tools.agent_clients`'s
 * `warnings_for()` does.
 */
export function conflictWarnings(table: ClientTable, chosen: readonly string[]): string[] {
  return [
    ...new Set(
      table.conflicts
        .filter((conflict) => conflict.clients.every((name) => chosen.includes(name)))
        .map((conflict) => conflict.warning),
    ),
  ];
}

/** Whether the *chosen* set meets the companion settings rule (`with` and any of `anyOf`). */
export function companionApplies(table: ClientTable, chosen: readonly string[]): boolean {
  const rule = table.copilotCompanionSettings;
  return chosen.includes(rule.with) && rule.anyOf.some((name) => chosen.includes(name));
}

/**
 * The clients the quick pick pre-picks in the editor host *host* (`vscode.env.uriScheme`):
 * the one row whose `preselectInHosts` names it (`kiro` in Kiro), else the table's
 * `default`.
 */
export function preselectedClients(table: ClientTable, host: string): string[] {
  const row = table.clients.find((client) => client.preselectInHosts.includes(host));
  return row === undefined ? [...table.default] : [row.name];
}

/** One quick-pick entry: the row's label, its name carried along, the host's pre-pick picked. */
export interface ClientPickItem {
  label: string;
  name: string;
  picked: boolean;
}

/**
 * The quick pick's entries, one per row in the table's order, the clients
 * `preselectedClients()` gives for *host* pre-picked.
 */
export function clientPickItems(table: ClientTable, host: string): ClientPickItem[] {
  const preselected = preselectedClients(table, host);
  return table.clients.map((client) => ({
    label: client.label,
    name: client.name,
    picked: preselected.includes(client.name),
  }));
}

/** The quick pick's title. */
export const CLIENT_PICK_TITLE = "SWCraft: which chat clients should this project be set up for?";

/** The line for a quick pick accepted with nothing picked. */
export const NO_CLIENT_CHOSEN_MESSAGE =
  "SWCraft: no chat client was chosen, so nothing was written; run Onboard Project again and pick at least one.";

/**
 * The client names the quick pick's answer chose, in the pick's order; `undefined` when it
 * was cancelled or accepted with nothing picked, and *Onboard Project* then writes nothing
 * (`swcraft init --client` has no "no client" choice either).
 */
export function chosenClients(
  picked: readonly Pick<ClientPickItem, "name">[] | undefined,
): string[] | undefined {
  if (picked === undefined || picked.length === 0) {
    return undefined;
  }
  return picked.map((item) => item.name);
}

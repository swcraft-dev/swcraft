// Layer 2: the review tab's half answers a live panel server.
//
// A real server (the repository's `.venv` interpreter running `-m swcraft_mcp.server` with
// `SWCRAFT_REVIEW=panel`, `SWCRAFT_MANIFEST` naming a copy of `examples/basic` in a temporary
// directory, and `HOME`, `LOCALAPPDATA` and `USERPROFILE` all naming that directory, so the
// SWCraft home and its `reviews/` are the test's own on every platform —
// `swcraft_tools/swcraft_home.py` reads `HOME` on POSIX, `LOCALAPPDATA` first on Windows)
// receives one `change_set` over newline-delimited JSON-RPC on stdio: an `update` of one
// requirement's `title`. While that call waits, this file does what `commands.ts` does, with
// `reviewCore.ts`'s own functions and nothing else: it keeps the live records of this window's manifest
// (`readReviewEntry`, `windowReviews`), builds the view's rows (`reviewNodes`) and the tab's
// page (`reviewHtml`), re-reads the record at click time, plans the answer (`answerPlan`) and
// writes it (`writeDecision`). The reviews directory is computed by the extension's rule
// (`toolchainHome` + `reviewsDir`) over the server's environment, so a drift between the two
// copies of the home rule fails here. An approval returns `committed` with the new title in
// the model file; a rejection returns `abandoned` with the file byte for byte unchanged.
//
// The process is driven as `packages/mcp/tests/test_client_stanzas.py` drives it: raw JSON-RPC
// lines (an SDK client would own the child's lifetime), a deadline per message, and a teardown
// that closes stdin, waits with a deadline and, past it or on any failure, ends and reaps the
// whole process tree. The child is started `detached`; whether a signal to its group reaches
// it is probed once with signal 0 to `-pid` (never a platform check), and the tree is ended
// with `process.kill(-pid, "SIGKILL")` where it does, `taskkill /T /F` otherwise.
//
// What it does **not** reach: the webview's rendering and buttons (the script's digest is
// computed here as `textDigest` of the page's source text), `commands.ts`'s wiring (the
// re-read timer, the tabs' transitions, the badge), VS Code itself, a human, and an MCP client
// in between (the test is the client). Windows paths are reached only on the Windows runner;
// this change set raises no safety question, so only the delta question is answered.
//
// `npm run test` therefore needs the repository's `.venv` with `swcraft_mcp` installed: true
// inside `dev.py test`; a bare `npm ci && npm run test` checkout FAILS (never skips), naming
// both interpreter paths and the `python scripts/dev.py install` remedy.

import assert from "node:assert/strict";
import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import { currentPlatform } from "./commandCore";
import {
  answerPlan,
  type Decision,
  escapeHtml,
  isPendingFileName,
  localUtcOffsetMinutes,
  newNonce,
  type PendingRecord,
  pendingFileName,
  REVIEW_HEADER,
  type ReviewEntry,
  readReviewEntry,
  reviewHtml,
  reviewNodes,
  reviewsDir,
  textDigest,
  textHtml,
  windowReviews,
  writeDecision,
} from "./reviewCore";
import { toolchainHome } from "./toolchain";

/** The repository root, from `vscode_extension/out/`. */
const REPO_ROOT = path.resolve(__dirname, "..", "..");

/** The project every case copies: the manifest and the model, nothing else. */
const EXAMPLE = path.join(REPO_ROOT, "examples", "basic");

/** How long the server may take to start, or to answer one message. */
const RESPONSE_MS = 60_000;

/** How long the server may take to hand its question to the reviews directory. */
const HAND_OFF_MS = 60_000;

/** How long the server may take to end once its stdin is closed, and a tree kill to be reaped. */
const EXIT_MS = 15_000;

/** The element the change set edits, its field, and the file that holds it. */
const ELEMENT = "REQ_EXAMPLE_001";
const ELEMENT_FILE = path.join("model", "requirements", `${ELEMENT}.yaml`);

/**
 * The variables a process needs to start at all, carried from the test's own
 * environment; `HOME`, `LOCALAPPDATA` and `USERPROFILE` are then overwritten.
 * No `SWCRAFT_*` variable is inherited, so the server is told only what this
 * file tells it (a scope in the parent's environment would change the review).
 */
const CARRIED_VARIABLES = [
  "PATH",
  "SYSTEMROOT",
  "WINDIR",
  "COMSPEC",
  "PATHEXT",
  "TEMP",
  "TMP",
  "TMPDIR",
  "LANG",
  "LC_ALL",
] as const;

// --- The interpreter ----------------------------------------------------------

/** The two places `dev.py install` puts the `.venv` interpreter, POSIX layout first. */
function venvCandidates(repoRoot: string): string[] {
  return [
    path.join(repoRoot, ".venv", "bin", "python"),
    path.join(repoRoot, ".venv", "Scripts", "python.exe"),
  ];
}

/**
 * The `.venv` interpreter of *repoRoot*: whichever candidate exists (a probe of
 * the file, never of the platform). None existing throws, naming both and the
 * remedy: this layer fails without the server, it never skips.
 */
function venvPython(repoRoot: string, exists: (file: string) => boolean): string {
  const candidates = venvCandidates(repoRoot);
  const found = candidates.find(exists);
  if (found === undefined) {
    throw new Error(
      `no repository interpreter at ${candidates.join(" or ")}: the panel loop test starts the ` +
        "real server from the repository's .venv with swcraft_mcp installed; run " +
        "`python scripts/dev.py install` first",
    );
  }
  return found;
}

test("an absent .venv fails, naming both interpreter paths and the dev.py install remedy", () => {
  const nowhere = path.join(os.tmpdir(), "swcraft-panel-loop-no-repo");
  assert.throws(
    () => venvPython(nowhere, () => false),
    (error: Error) => {
      for (const candidate of venvCandidates(nowhere)) {
        assert.ok(error.message.includes(candidate), error.message);
      }
      assert.ok(error.message.includes("python scripts/dev.py install"), error.message);
      return true;
    },
  );
  assert.equal(
    venvPython(nowhere, (file) => file.endsWith("python.exe")),
    venvCandidates(nowhere)[1],
    "the Windows layout is taken when it is the one that exists",
  );
});

// --- The process ----------------------------------------------------------------

type Message = Record<string, unknown>;

/** One started server, spoken to over newline-delimited JSON-RPC. */
class Server {
  private buffer = "";
  private readonly messages: Message[] = [];
  private readonly waiters: Array<() => void> = [];
  private nextId = 0;
  private stdoutEnded = false;
  /** Whether a signal to `-pid` reaches the child: the probe, asked once at start. */
  readonly groupSignals: boolean;
  readonly exited: Promise<void>;
  private hasExited = false;
  /** The spawn's own failure (an interpreter that cannot be executed), which ends every wait. */
  private spawnError: Error | undefined;

  constructor(
    readonly child: ChildProcess,
    readonly stderrFile: string,
  ) {
    this.exited = new Promise((resolve) => {
      child.once("exit", () => {
        this.hasExited = true;
        resolve();
      });
      // Without a listener a failed spawn's `error` crashes the test runner; with
      // it the process counts as ended and the pending request fails with the cause.
      child.once("error", (error) => {
        this.spawnError = error;
        this.hasExited = true;
        resolve();
        this.wake();
      });
    });
    // A write to the stdin of a process that never started fails on the pipe; the
    // request then fails with the spawn's error above, not the runner.
    child.stdin?.on("error", () => undefined);
    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => {
      this.buffer += chunk;
      let newline = this.buffer.indexOf("\n");
      while (newline >= 0) {
        const line = this.buffer.slice(0, newline).trim();
        this.buffer = this.buffer.slice(newline + 1);
        if (line !== "") {
          this.messages.push(JSON.parse(line) as Message);
        }
        newline = this.buffer.indexOf("\n");
      }
      this.wake();
    });
    child.stdout?.on("end", () => {
      this.stdoutEnded = true;
      this.wake();
    });
    this.groupSignals = probeGroupSignal(child.pid);
  }

  get ended(): boolean {
    return this.hasExited;
  }

  private wake(): void {
    for (const waiter of this.waiters.splice(0)) {
      waiter();
    }
  }

  stderrTail(): string {
    try {
      return fs.readFileSync(this.stderrFile, { encoding: "utf8" }).slice(-2000);
    } catch (error) {
      return `<stderr unreadable: ${(error as NodeJS.ErrnoException).code}>`;
    }
  }

  private send(message: Message): void {
    this.child.stdin?.write(`${JSON.stringify(message)}\n`, "utf8");
  }

  notify(method: string): void {
    this.send({ jsonrpc: "2.0", method });
  }

  /** Send one request; the returned promise is its result within *deadlineMs*. */
  request(method: string, params: Message, deadlineMs = RESPONSE_MS): Promise<Message> {
    this.nextId += 1;
    const id = this.nextId;
    this.send({ jsonrpc: "2.0", id, method, params });
    return this.response(id, method, deadlineMs);
  }

  private async response(id: number, method: string, deadlineMs: number): Promise<Message> {
    const deadline = Date.now() + deadlineMs;
    for (;;) {
      const at = this.messages.findIndex(
        (message) => message.id === id && !Object.hasOwn(message, "method"),
      );
      if (at >= 0) {
        const [answer] = this.messages.splice(at, 1);
        assert.equal(answer.error, undefined, `${method} answered ${JSON.stringify(answer.error)}`);
        return answer.result as Message;
      }
      assert.equal(
        this.spawnError,
        undefined,
        `the server could not be started for ${method}: ${String(this.spawnError)}`,
      );
      assert.ok(
        !this.stdoutEnded,
        `the server closed its stdout before answering ${method}; stderr ends:\n${this.stderrTail()}`,
      );
      const remaining = deadline - Date.now();
      assert.ok(
        remaining > 0,
        `no answer to ${method} within ${deadlineMs} ms; stderr ends:\n${this.stderrTail()}`,
      );
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, remaining);
        this.waiters.push(() => {
          clearTimeout(timer);
          resolve();
        });
      });
    }
  }

  async initialize(): Promise<Message> {
    const result = await this.request("initialize", {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "panel-loop-test", version: "1" },
    });
    this.notify("notifications/initialized");
    return result;
  }

  call(name: string, args: Message, deadlineMs = RESPONSE_MS): Promise<Message> {
    return this.request("tools/call", { name, arguments: args }, deadlineMs);
  }
}

/** Whether signal 0 to the child's process group is delivered: the capability probe. */
function probeGroupSignal(pid: number | undefined): boolean {
  if (pid === undefined) {
    return false;
  }
  try {
    process.kill(-pid, 0);
    return true;
  } catch {
    return false;
  }
}

/** Resolve after *ms*, or when *promise* settles, whichever is first; true when it settled. */
async function within(promise: Promise<void>, ms: number): Promise<boolean> {
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<boolean>((resolve) => {
    timer = setTimeout(() => resolve(false), ms);
  });
  try {
    return await Promise.race([promise.then(() => true), timeout]);
  } finally {
    clearTimeout(timer);
  }
}

/**
 * End *server*'s process and its descendants, then reap it with a deadline.
 *
 * What it does not reach: a POSIX descendant that started a session of its
 * own, and a Windows descendant whose parent exited before `taskkill` walked
 * the tree.
 */
async function endTree(server: Server): Promise<void> {
  const pid = server.child.pid;
  if (pid !== undefined && !server.ended) {
    if (server.groupSignals) {
      try {
        process.kill(-pid, "SIGKILL");
      } catch {
        // The group is gone already.
      }
    } else {
      spawnSync("taskkill", ["/T", "/F", "/PID", String(pid)], {
        stdio: "ignore",
        timeout: EXIT_MS,
        windowsHide: true,
      });
    }
    try {
      server.child.kill("SIGKILL");
    } catch {
      // Already reaped.
    }
  }
  assert.ok(
    await within(server.exited, EXIT_MS),
    "the server was not reaped after its tree was ended",
  );
}

interface Started {
  server: Server;
  project: string;
  manifest: string;
  reviews: string;
}

/**
 * Run *body* against a started server over a fresh copy of the example in a
 * temporary directory that is also the server's home. On leaving: stdin closed
 * and the exit awaited with a deadline; past it, or when *body* failed, the
 * tree is ended; then the directory is removed. *interpreter* replaces the
 * `.venv`'s, for the case of one that cannot be executed.
 */
async function withServer(
  body: (started: Started) => Promise<void>,
  interpreter?: string,
): Promise<void> {
  const python = interpreter ?? venvPython(REPO_ROOT, (file) => fs.existsSync(file));
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-panel-loop-"));
  try {
    const home = path.join(root, "home");
    const project = path.join(root, "project");
    fs.mkdirSync(home);
    fs.mkdirSync(project);
    fs.copyFileSync(path.join(EXAMPLE, "swcraft.yaml"), path.join(project, "swcraft.yaml"));
    fs.cpSync(path.join(EXAMPLE, "model"), path.join(project, "model"), { recursive: true });
    const manifest = path.join(project, "swcraft.yaml");

    const env: NodeJS.ProcessEnv = {};
    for (const name of CARRIED_VARIABLES) {
      if (process.env[name] !== undefined) {
        env[name] = process.env[name];
      }
    }
    Object.assign(env, {
      HOME: home,
      LOCALAPPDATA: home,
      USERPROFILE: home,
      SWCRAFT_REVIEW: "panel",
      SWCRAFT_MANIFEST: manifest,
    });
    // The extension's rule over the server's environment: what `commands.ts`
    // would read in a window whose user home is *home*.
    const platform = currentPlatform();
    const reviews = reviewsDir(toolchainHome(env, home, platform), platform);

    const stderrFile = path.join(root, "server-stderr.txt");
    const stderrFd = fs.openSync(stderrFile, "w");
    let child: ChildProcess;
    try {
      child = spawn(python, ["-m", "swcraft_mcp.server"], {
        cwd: project,
        env,
        stdio: ["pipe", "pipe", stderrFd],
        detached: true,
        windowsHide: true,
      });
    } finally {
      fs.closeSync(stderrFd);
    }
    const server = new Server(child, stderrFile);
    let failed = true;
    let bodyError: unknown;
    try {
      await body({ server, project, manifest, reviews });
      failed = false;
    } catch (error) {
      bodyError = error;
    }
    let teardownError: unknown;
    try {
      child.stdin?.end();
      const exited = !failed && (await within(server.exited, EXIT_MS));
      if (!exited) {
        await endTree(server);
      }
      if (!failed) {
        assert.ok(exited, `the server did not exit within ${EXIT_MS} ms of its stdin closing`);
      }
    } catch (error) {
      teardownError = error;
    }
    // A teardown failure is raised only when the body passed; after a failing body
    // it is logged, so it never masks the failure that matters.
    if (failed) {
      if (teardownError !== undefined) {
        console.error(
          `panelLoop teardown also failed after the body failed: ${String(teardownError)}`,
        );
      }
      throw bodyError;
    }
    if (teardownError !== undefined) {
      throw teardownError;
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

// --- The tool replies -----------------------------------------------------------

/** The JSON body of a `tools/call` result: its structured content, else its first text block. */
function body(result: Message): Message {
  assert.notEqual(result.isError, true, `the call failed: ${JSON.stringify(result.content)}`);
  if (typeof result.structuredContent === "object" && result.structuredContent !== null) {
    return result.structuredContent as Message;
  }
  const content = result.content as Array<{ text?: string }>;
  return JSON.parse(content[0]?.text ?? "") as Message;
}

// --- The review tab's half ------------------------------------------------------

/** The live records of *manifest*'s project in *dir*, read as `commands.ts` reads them. */
function liveRecords(dir: string, manifest: string): PendingRecord[] {
  let names: string[];
  try {
    names = fs.readdirSync(dir);
  } catch {
    return [];
  }
  const entries: ReviewEntry[] = [];
  for (const name of names.filter(isPendingFileName)) {
    const entry = readReviewEntry(fs, dir, name);
    if (entry !== undefined) {
      entries.push(entry);
    }
  }
  return windowReviews(
    entries,
    [fs.realpathSync.native(manifest)],
    currentPlatform(),
    Date.now(),
    (recorded) => {
      try {
        return fs.realpathSync.native(recorded);
      } catch {
        return recorded;
      }
    },
  );
}

/** The one live record, waited for while the server's call is outstanding. */
async function waitForQuestion(
  server: Server,
  dir: string,
  manifest: string,
): Promise<PendingRecord> {
  const deadline = Date.now() + HAND_OFF_MS;
  for (;;) {
    const records = liveRecords(dir, manifest);
    if (records.length > 0) {
      assert.equal(records.length, 1, "one change set, one question");
      return records[0];
    }
    assert.ok(
      Date.now() < deadline && !server.ended,
      `no pending question within ${HAND_OFF_MS} ms; server stderr ends:\n${server.stderrTail()}`,
    );
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}

/**
 * Answer the live question as the review tab does: the view's row and the
 * tab's page built from the record, the record re-read at click time, the plan
 * made and the decision written. Returns the record answered.
 */
async function answer(
  started: Started,
  intent: string,
  newTitle: string,
  decision: Decision,
): Promise<PendingRecord> {
  const shown = await waitForQuestion(started.server, started.reviews, started.manifest);
  assert.equal(shown.question, "delta");
  assert.equal(shown.intent, intent);
  assert.ok(shown.text.includes(newTitle), `the question shows the new value:\n${shown.text}`);

  const offset = localUtcOffsetMinutes(shown.created);
  const [row, ...others] = reviewNodes([shown], offset);
  assert.deepEqual(others, []);
  assert.equal(row.changeSetId, shown.change_set_id);
  assert.equal(row.label, intent, "the Pending Change Sets row is labelled with the intent");

  const page = reviewHtml(shown, { nonce: newNonce(), utcOffsetMinutes: offset });
  assert.ok(page.includes(`<h1>${REVIEW_HEADER}</h1>`), page);
  assert.ok(page.includes(`<pre id="text">\n${textHtml(shown.text, shown.warning_lines)}</pre>`));
  assert.ok(page.includes(escapeHtml(newTitle)), "the tab's page carries the new value");

  // The click: the record is re-read now, and the tab's script would post the
  // digest of the text it showed, which is the record's text.
  const entry = readReviewEntry(fs, started.reviews, pendingFileName(shown));
  assert.ok(entry !== undefined, "the question is still on disk at click time");
  const [fresh] = windowReviews(
    [entry],
    [fs.realpathSync.native(started.manifest)],
    currentPlatform(),
    Date.now(),
    (recorded) => fs.realpathSync.native(recorded),
  );
  const plan = answerPlan(shown, fresh, { decision, digest: textDigest(shown.text) });
  assert.ok(plan.ok, JSON.stringify(plan));
  const written = writeDecision(fs, started.reviews, plan.record, plan.decision, plan.digest);
  assert.ok(written.ok, JSON.stringify(written));
  return shown;
}

interface Outcome {
  status: unknown;
  before: Buffer;
  after: Buffer;
  newTitle: string;
  left: string[];
}

/** Start the server, propose the title change, answer it with *decision*; returns the outcome. */
async function proposeAndAnswer(decision: Decision): Promise<Outcome> {
  let outcome: Outcome | undefined;
  await withServer(async (started) => {
    const { server, project, reviews } = started;
    const initialized = await server.initialize();
    assert.equal((initialized.serverInfo as Message).name, "swcraft");

    const discovered = body(await server.call("discover", {}));
    const baseline = discovered.baseline;
    assert.equal(typeof baseline, "string", JSON.stringify(discovered));

    const file = path.join(project, ELEMENT_FILE);
    const before = fs.readFileSync(file);
    const newTitle = `Panel loop ${decision} title`;
    const intent = `Retitle ${ELEMENT} (${decision})`;
    // Sent now, answered only after the decision: the panel wait has no deadline
    // of its own, so the deadline is the hand-off's plus one response.
    const reply = server.call(
      "change_set",
      {
        baseline,
        intent,
        operations: [
          {
            operation: "update",
            address: { layer: "requirements", element: ELEMENT, path: [{ field: "title" }] },
            value: newTitle,
          },
        ],
      },
      HAND_OFF_MS + RESPONSE_MS,
    );
    // A rejection of the reply while the question is answered is awaited below,
    // not left unhandled.
    reply.catch(() => undefined);

    await answer(started, intent, newTitle, decision);
    const changed = body(await reply);
    const left = fs.existsSync(reviews) ? fs.readdirSync(reviews).filter(isPendingFileName) : [];
    outcome = { status: changed.status, before, after: fs.readFileSync(file), newTitle, left };
  });
  assert.ok(outcome !== undefined, "the body returned without an outcome");
  return outcome;
}

test("an approval written by reviewCore commits the live server's change set", async () => {
  const { status, before, after, newTitle, left } = await proposeAndAnswer("approve");
  assert.equal(status, "committed");
  assert.notDeepEqual(after, before);
  assert.ok(after.toString("utf8").includes(`title: ${newTitle}`), after.toString("utf8"));
  assert.deepEqual(left, [], "the answered question was withdrawn");
});

test("a rejection written by reviewCore abandons it and leaves the model file untouched", async () => {
  const { status, before, after, left } = await proposeAndAnswer("reject");
  assert.equal(status, "abandoned");
  assert.deepEqual(after, before, "the model file is byte for byte unchanged");
  assert.deepEqual(left, [], "the answered question was withdrawn");
});

test("an interpreter that cannot be executed fails the call, never the test runner", async () => {
  const missing = path.join(os.tmpdir(), `swcraft-panel-loop-no-such-python-${process.pid}`);
  await assert.rejects(
    withServer(async ({ server }) => {
      await server.initialize();
    }, missing),
    /could not be started for initialize/,
  );
});

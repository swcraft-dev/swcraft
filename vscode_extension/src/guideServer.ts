// `SWCraft: Open Guide` — the one SWCraft command whose CLI child (`swcraft
// docs`) is not expected to exit.
//
// Not routed through `spawnCli()`, which resolves on `close`: `swcraft docs`
// serves until interrupted, so here the *announcement* of its address resolves
// the run.
//
// No `vscode` import, so it runs under plain `node --test`. It spawns and writes
// only through the injected `GuideSpawner` and `GuideChannel`; the adapter
// (commands.ts) notifies and opens the browser.

import { SWCRAFT_COMMAND_NAME } from "./toolchain";

/** The `swcraft` subcommand the guide is served by, as `cli/docs_command.py`'s parser spells it. */
const DOCS_SUBCOMMAND = "docs";

/**
 * How long to wait for the child to announce its address before killing it.
 * Generous (the announcement measured at 0.37 s on Linux; Windows unmeasured):
 * it only turns a child that starts and says nothing into a named failure.
 */
export const GUIDE_ANNOUNCE_TIMEOUT_MS = 20_000;

/**
 * The address `cmd_docs` announces, matched on the accumulated stdout. Relies on
 * three properties `test_cli.py` pins: the host is always `127.0.0.1` (a general
 * URL pattern could be steered into opening something else), the port is the
 * bound one, and the line reaches the pipe while the child serves. The trailing
 * slash (printed last) is required, so a port split across chunks never matches.
 */
const ANNOUNCED_URL = /http:\/\/127\.0\.0\.1:\d{1,5}\//;

/**
 * The argv (excluding the executable, the `swcraft` that `toolchain.ts`
 * resolves) that serves the installed guide: `swcraft docs --no-browser`.
 *
 * `--no-browser` because the CLI would open a browser on the extension host's
 * machine, which in a Remote SSH, WSL or Codespaces window is not the user's;
 * the extension maps the URL through `vscode.env.asExternalUri` and opens it
 * itself. No `--port`: the default `0` lets each window get a free port.
 */
export function buildDocsArgv(): string[] {
  return [DOCS_SUBCOMMAND, "--no-browser"];
}

/** The address in *stdout*, or `undefined` while the child has not announced one. */
export function findAnnouncedUrl(stdout: string): string | undefined {
  return ANNOUNCED_URL.exec(stdout)?.[0];
}

/** Where one guide run is spawned from: which executable, in which directory. */
export interface GuideSpawnContext {
  executable: string;
  cwd: string;
}

/**
 * The slice of `child_process.ChildProcess` this module uses, declared
 * structurally so the tests can pass a fake child without a cast.
 */
export interface GuideChildProcess {
  readonly stdout: { on(event: "data", listener: (chunk: Buffer) => void): unknown } | null;
  readonly stderr: { on(event: "data", listener: (chunk: Buffer) => void): unknown } | null;
  on(event: "error", listener: (error: Error) => void): unknown;
  on(event: "close", listener: (code: number | null) => void): unknown;
  kill(): unknown;
}

/** How the adapter starts the child: the real `spawn()`, or a fake under test. */
export type GuideSpawner = (
  context: GuideSpawnContext,
  argv: readonly string[],
) => GuideChildProcess;

/**
 * What one `SWCraft: Open Guide` invocation produced. `message`, on every
 * failing outcome, is the notification text.
 *
 * - `serving` — an address was announced; `reused` when a server was already
 *   running and nothing was started;
 * - `spawn-failed` — the CLI could not be launched at all;
 * - `refused` — the CLI exited before announcing an address (typically an
 *   install without a guide payload); `detail` is its stderr, verbatim;
 * - `no-announcement` — neither announced nor exited within
 *   `GUIDE_ANNOUNCE_TIMEOUT_MS`, and killed;
 * - `stopped` — the session was disposed before or during the start; any child
 *   was killed and there is nothing to tell the user.
 *
 * `shared` marks a failure received by waiting on another invocation's start:
 * only the starter notifies, so two clicks give one notification.
 */
export type GuideOutcome =
  | { kind: "serving"; url: string; reused: boolean }
  | { kind: "spawn-failed"; message: string; shared?: true }
  | {
      kind: "refused";
      message: string;
      detail: string;
      exitCode: number | null;
      shared?: true;
    }
  | { kind: "no-announcement"; message: string; shared?: true }
  | { kind: "stopped" };

/** The outcome of an invocation that a disposed session refused to start, or stopped. */
const STOPPED: GuideOutcome = { kind: "stopped" };

/** Everything the child writes, handed on as it arrives (the output channel, in production). */
export type GuideLogger = (text: string) => void;

/**
 * The output channel this command alone writes to: `swcraft docs` logs one line
 * per HTTP request for as long as it serves, which in the shared `SWCraft`
 * channel would interleave with every later command's output.
 */
export const GUIDE_OUTPUT_CHANNEL_NAME = "SWCraft Guide";

/** The slice of `vscode.OutputChannel` the guide command uses, declared structurally. */
export interface GuideChannel {
  append(text: string): void;
  show(preserveFocus: boolean): void;
}

/**
 * Serve the guide, routing everything the run produces to *channel* (the
 * `GUIDE_OUTPUT_CHANNEL_NAME` one, never the shared channel): the `$ <command>`
 * echo, the child's output as it arrives, and on a failure the reveal.
 */
export async function serveGuideTo(
  session: GuideSession,
  context: GuideSpawnContext,
  channel: GuideChannel,
): Promise<GuideOutcome> {
  const outcome = await session.open(context, (text) => channel.append(text));
  // A disposed session has nothing to reveal, and a waiter on a failed start
  // leaves the reveal to the starter (`GuideOutcome`'s `shared`).
  if (outcome.kind !== "serving" && outcome.kind !== "stopped" && outcome.shared !== true) {
    // Shown, not merely appended: the refusal is several lines a notification
    // cannot carry. Success reveals nothing; it ends in the guide's page.
    channel.show(true);
  }
  return outcome;
}

export function guideSpawnFailedMessage(executable: string): string {
  return (
    `SWCraft: the CLI could not be started (${executable}), so the user guide could not be ` +
    "opened — no SWCraft command can run until that is fixed. Run `SWCraft: Check Editor Tooling`."
  );
}

/**
 * The notification for a CLI that exited before it announced an address: the
 * first non-empty line of stderr, and where the rest is (a notification
 * collapses newlines and truncates).
 */
export function guideRefusedMessage(exitCode: number | null, stderr: string): string {
  const headline = stderr
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line !== "");
  const code = exitCode === null ? "without an exit code" : `with exit code ${exitCode}`;
  const detail = headline === undefined ? "" : ` ${headline}`;
  return (
    `SWCraft: the user guide could not be served — the CLI exited ${code}.${detail} ` +
    `See the ${GUIDE_OUTPUT_CHANNEL_NAME} output channel for the full message.`
  );
}

export function guideNoAnnouncementMessage(timeoutMs: number): string {
  return (
    `SWCraft: the CLI did not announce the guide's address within ${Math.round(timeoutMs / 1000)} ` +
    `seconds, so it was stopped. See the ${GUIDE_OUTPUT_CHANNEL_NAME} output channel for ` +
    "anything it did write."
  );
}

/** A guide server this session started and has not stopped. */
interface RunningGuide {
  url: string;
  child: GuideChildProcess;
}

/**
 * The one guide server a window may have running, and its lifecycle.
 *
 * - One server per window, reused: a second open answers the running address.
 * - A start in flight is shared, so two quick clicks spawn one server (two ports
 *   would defeat the adapter's tab reuse); a failed start is forgotten.
 * - A child that exits on its own clears the session, so the next open starts
 *   afresh.
 * - Stopped on `dispose()` (from `context.subscriptions`), since the extension
 *   host's exit does not kill a spawned child. The child is held in `pending`
 *   until it announces, so a dispose during a start kills it too. After
 *   `dispose()` every open answers `stopped` without spawning.
 */
export class GuideSession {
  private running: RunningGuide | undefined;
  private starting: Promise<GuideOutcome> | undefined;
  private pending: GuideChildProcess | undefined;
  private disposed = false;

  constructor(
    private readonly spawner: GuideSpawner,
    private readonly timeoutMs: number = GUIDE_ANNOUNCE_TIMEOUT_MS,
  ) {}

  /** The address of the running server, or `undefined` when none is running. */
  get url(): string | undefined {
    return this.running?.url;
  }

  /**
   * Serve the guide and answer with its address: reuse the running server, or
   * spawn `swcraft docs --no-browser` and wait for the announcement. *log*
   * receives the command line and every byte of the child's output.
   */
  async open(context: GuideSpawnContext, log: GuideLogger): Promise<GuideOutcome> {
    if (this.disposed) {
      return STOPPED;
    }
    const running = this.running;
    if (running !== undefined) {
      return { kind: "serving", url: running.url, reused: true };
    }
    const starting = this.starting;
    if (starting !== undefined) {
      // Share another invocation's start; a failure is the starter's to report.
      const outcome = await starting;
      if (outcome.kind === "serving") {
        return { ...outcome, reused: true };
      }
      return outcome.kind === "stopped" ? outcome : { ...outcome, shared: true };
    }
    const start = this.start(context, log);
    this.starting = start;
    try {
      return await start;
    } finally {
      if (this.starting === start) {
        this.starting = undefined;
      }
    }
  }

  /** Spawn `swcraft docs --no-browser` and wait for its announcement. */
  private async start(context: GuideSpawnContext, log: GuideLogger): Promise<GuideOutcome> {
    const argv = buildDocsArgv();
    log(`$ ${SWCRAFT_COMMAND_NAME} ${argv.join(" ")}\n`);
    const child = this.spawner(context, argv);
    this.pending = child;
    const outcome = await readAnnouncement(child, context.executable, log, this.timeoutMs);
    if (this.pending !== child) {
      // `stop()` killed the child while this start waited; hand nothing out.
      return STOPPED;
    }
    this.pending = undefined;
    if (outcome.kind !== "serving") {
      return outcome;
    }
    this.running = { url: outcome.url, child };
    // Armed only now: before the announcement `close` is the refusal signal
    // `readAnnouncement()` owns.
    child.on("close", () => {
      if (this.running?.child === child) {
        this.running = undefined;
      }
    });
    return outcome;
  }

  /**
   * Stop the running server and the start in flight, if any. Idempotent; safe
   * on a child that already exited. A stopped start answers `stopped`.
   */
  stop(): void {
    const running = this.running;
    const pending = this.pending;
    this.running = undefined;
    this.pending = undefined;
    running?.child.kill();
    pending?.kill();
  }

  /**
   * `vscode.Disposable`'s shape, so the session can be pushed onto
   * `context.subscriptions`. Terminal: every later `open()` answers `stopped`.
   */
  dispose(): void {
    this.disposed = true;
    this.stop();
  }
}

/**
 * Wait for the child to announce an address, exit, or say nothing for too long.
 *
 * The announcement reaches the pipe only because `cmd_docs` flushes it
 * (`test_cli_docs.py::TestCliDocsStreaming`); nothing on this side can detect its loss.
 */
function readAnnouncement(
  child: GuideChildProcess,
  executable: string,
  log: GuideLogger,
  timeoutMs: number,
): Promise<GuideOutcome> {
  return new Promise<GuideOutcome>((resolve) => {
    let stdout = "";
    let stderr = "";
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const finish = (outcome: GuideOutcome): void => {
      if (settled) {
        return;
      }
      settled = true;
      if (timer !== undefined) {
        clearTimeout(timer);
      }
      resolve(outcome);
    };

    timer = setTimeout(() => {
      // Killed: it holds a bound socket whose address nobody will ever know.
      child.kill();
      finish({ kind: "no-announcement", message: guideNoAnnouncementMessage(timeoutMs) });
    }, timeoutMs);

    child.stdout?.on("data", (chunk: Buffer) => {
      const text = chunk.toString();
      stdout += text;
      log(text);
      const url = findAnnouncedUrl(stdout);
      if (url !== undefined) {
        finish({ kind: "serving", url, reused: false });
      }
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      const text = chunk.toString();
      stderr += text;
      log(text);
    });
    child.on("error", (error) => {
      log(`${error.message}\n`);
      finish({ kind: "spawn-failed", message: guideSpawnFailedMessage(executable) });
    });
    child.on("close", (code) => {
      // Reached only before an announcement. Exit code 0 is a refusal too: a
      // serving command does not exit at all.
      finish({
        kind: "refused",
        message: guideRefusedMessage(code, stderr),
        detail: stderr,
        exitCode: code,
      });
    });
  });
}

// `SWCraft: Open Guide`'s pure half (guideServer.ts): the argv, the announcement parser, the
// outcome of one run and the session's lifecycle.
//
// The fake child is a plain `EventEmitter` plus two `PassThrough` streams: it satisfies
// `GuideChildProcess` structurally, without a cast, and delivers stdout in chunks of a case's
// choosing — the one property of the real pipe the parser depends on.
//
// What this file does not reach: nothing here spawns python, so whether `swcraft docs` prints
// what `findAnnouncedUrl()` expects is pinned on the Python side by
// `packages/tools/tests/test_cli_docs.py::TestCliDocsStreaming`; the announcement fixture below is
// a verbatim copy of that command's output, kept in step by review alone. Nor does it reach
// the open (the Simple Browser, `vscode.env.openExternal` as its fallback), the notifications,
// or which output channel the adapter hands `serveGuideTo()` — those are commands.ts's,
// pinned by source scans in `commandCore.test.ts`.
//
// No `vscode` import.
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { test } from "node:test";

import {
  buildDocsArgv,
  findAnnouncedUrl,
  GUIDE_ANNOUNCE_TIMEOUT_MS,
  GUIDE_OUTPUT_CHANNEL_NAME,
  type GuideChannel,
  type GuideChildProcess,
  GuideSession,
  type GuideSpawnContext,
  guideNoAnnouncementMessage,
  guideRefusedMessage,
  guideSpawnFailedMessage,
  serveGuideTo,
} from "./guideServer";

/**
 * What `swcraft docs --no-browser` writes before it starts serving: three lines, the address
 * on the second, indented by two spaces. Copied verbatim from a real run (Linux, the
 * repository's own `.venv`, guide payload installed).
 */
const ANNOUNCEMENT =
  "Serving the SWCraft guide from /opt/venv/lib/python3.12/site-packages/swcraft_tools/guide_site\n" +
  "  http://127.0.0.1:43773/\n" +
  "Press Ctrl+C to stop.\n";

/** The refusal `guide_site.py` composes when no install carries a guide, abridged to its shape. */
const REFUSAL =
  "Error: This copy of SWCraft carries no user guide to serve.\n" +
  "\n" +
  "  looked for: /opt/venv/lib/python3.12/site-packages/swcraft_tools/guide_site\n" +
  "\n" +
  "An installed release of the swcraft-tools package carries the guide at that location.\n" +
  "Otherwise take swcraft-guide-0.5.1.zip from the release, unpack it, and run the command again\n" +
  "with SWCRAFT_GUIDE_SITE set to the unpacked directory.\n";

/**
 * What the running server writes for every page a reader opens: one line per request, on
 * **stderr**, for as long as the window is up. Copied verbatim from a native Windows run.
 * Streamed into the shared `SWCraft` channel, this traffic lands between the lines of every
 * later command; `cli.cmd_docs` prints it deliberately, and the routing is the extension's.
 */
const REQUEST_LOG =
  '127.0.0.1 - - [27/Aug/2026 13:02:22] "GET /reference/ HTTP/1.1" 304 -\n' +
  '127.0.0.1 - - [27/Aug/2026 13:02:25] "GET /reference/model/interfaces/ HTTP/1.1" 200 -\n';

const CONTEXT: GuideSpawnContext = { executable: "/w/.swcraft/current/bin/swcraft", cwd: "/w" };

/**
 * An output channel that remembers what it was given. Satisfies `GuideChannel` structurally,
 * since neither this file nor the module under test can load `vscode`.
 */
class RecordingChannel implements GuideChannel {
  readonly appended: string[] = [];
  shown = 0;
  lastPreserveFocus: boolean | undefined;

  append(text: string): void {
    this.appended.push(text);
  }

  show(preserveFocus: boolean): void {
    this.shown += 1;
    this.lastPreserveFocus = preserveFocus;
  }

  get text(): string {
    return this.appended.join("");
  }
}

/** A fake `swcraft docs` child a case drives by hand. */
class FakeChild extends EventEmitter implements GuideChildProcess {
  readonly stdout = new PassThrough();
  readonly stderr = new PassThrough();
  killed = 0;

  kill(): boolean {
    this.killed += 1;
    // The real `kill()` does not synthesise a `close` synchronously either —
    // a case that wants the child gone emits it itself.
    return true;
  }

  /** Write on stdout and yield to the event loop, as a real pipe chunk would. */
  async say(text: string): Promise<void> {
    this.stdout.write(text);
    await new Promise((resolve) => setImmediate(resolve));
  }

  async complain(text: string): Promise<void> {
    this.stderr.write(text);
    await new Promise((resolve) => setImmediate(resolve));
  }
}

/**
 * A session whose spawner hands back *children* in order, recording every
 * spawn. The recorded argv is what the `--no-browser` assertions read.
 */
function sessionOver(
  children: FakeChild[],
  timeoutMs = 5_000,
): {
  session: GuideSession;
  spawns: Array<{ context: GuideSpawnContext; argv: readonly string[] }>;
} {
  const spawns: Array<{ context: GuideSpawnContext; argv: readonly string[] }> = [];
  const queue = [...children];
  const session = new GuideSession((context, argv) => {
    spawns.push({ context, argv });
    const next = queue.shift();
    assert.ok(next !== undefined, "the session spawned more children than the case provided");
    return next;
  }, timeoutMs);
  return { session, spawns };
}

// --- buildDocsArgv -----------------------------------------------------------

test("buildDocsArgv: runs the swcraft executable and never opens a browser itself", () => {
  assert.deepEqual(buildDocsArgv(), ["docs", "--no-browser"]);
});

test("buildDocsArgv: passes no --project and no --port", () => {
  // `swcraft docs` accepts neither: it serves the install's own guide, and the
  // CLI's default port of 0 is what lets a second window bind its own.
  const argv = buildDocsArgv();
  assert.equal(argv.includes("--project"), false);
  assert.equal(argv.includes("--port"), false);
});

// --- findAnnouncedUrl --------------------------------------------------------

test("findAnnouncedUrl: reads the address out of the three announced lines", () => {
  assert.equal(findAnnouncedUrl(ANNOUNCEMENT), "http://127.0.0.1:43773/");
});

test("findAnnouncedUrl: nothing announced yet is undefined, not an empty string", () => {
  assert.equal(findAnnouncedUrl(""), undefined);
  assert.equal(findAnnouncedUrl("Serving the SWCraft guide from /somewhere\n"), undefined);
});

test("findAnnouncedUrl: a half-delivered address does not match until its trailing slash lands", () => {
  // The property the trailing `/` in the pattern buys: a chunk boundary inside
  // the port number must never yield a URL with a truncated port, which would
  // be a perfectly well-formed address pointing at the wrong (or no) server.
  const partial = "Serving the SWCraft guide from /x\n  http://127.0.0.1:4377";
  assert.equal(findAnnouncedUrl(partial), undefined);
  assert.equal(findAnnouncedUrl(`${partial}3/\n`), "http://127.0.0.1:43773/");
});

test("findAnnouncedUrl: only the loopback address is accepted", () => {
  // The parsed string is handed to `vscode.env.asExternalUri` and the Simple
  // Browser (`openExternal` as the fallback), so the pattern is pinned to the
  // host `cli.DOCS_BIND_HOST` fixes and to no other. The CLI has
  // no `--bind` option (pinned on the Python side); if it ever grows one, this
  // is the line that has to change with it.
  assert.equal(findAnnouncedUrl("  http://10.0.0.9:8000/\n"), undefined);
  assert.equal(findAnnouncedUrl("  http://example.test:8000/\n"), undefined);
  assert.equal(findAnnouncedUrl("  https://127.0.0.1:8000/\n"), undefined);
});

// --- the happy path ----------------------------------------------------------

test("open: spawns with --no-browser and answers with the address the child announced", async () => {
  const child = new FakeChild();
  const { session, spawns } = sessionOver([child]);
  const logged: string[] = [];

  const running = session.open(CONTEXT, (text) => logged.push(text));
  await child.say(ANNOUNCEMENT);
  const outcome = await running;

  assert.equal(spawns.length, 1);
  assert.deepEqual(spawns[0].argv, ["docs", "--no-browser"]);
  assert.deepEqual(spawns[0].context, CONTEXT);
  assert.deepEqual(outcome, { kind: "serving", url: "http://127.0.0.1:43773/", reused: false });
  assert.equal(session.url, "http://127.0.0.1:43773/");
  // Everything the child said reached the log, headed by the command line.
  assert.match(logged[0], /^\$ swcraft docs --no-browser$/m);
  assert.ok(logged.join("").includes("Press Ctrl+C to stop."));
});

test("open: resolves as soon as the address arrives, without waiting for the child to exit", async () => {
  // The whole reason this command cannot go through `spawnCli()`: the child of
  // a successful run never emits `close`, so an implementation that waited for
  // one would hang here rather than fail.
  const child = new FakeChild();
  const { session } = sessionOver([child]);
  const running = session.open(CONTEXT, () => {});
  await child.say(ANNOUNCEMENT);
  const outcome = await running;
  assert.equal(outcome.kind, "serving");
});

test("open: an address split across two chunks is still read", async () => {
  const child = new FakeChild();
  const { session } = sessionOver([child]);
  const running = session.open(CONTEXT, () => {});
  await child.say("Serving the SWCraft guide from /x\n  http://127.0.0.1:518");
  await child.say("81/\nPress Ctrl+C to stop.\n");
  assert.deepEqual(await running, {
    kind: "serving",
    url: "http://127.0.0.1:51881/",
    reused: false,
  });
});

// --- one server per window ---------------------------------------------------

test("open: a second invocation reuses the running server and spawns nothing", async () => {
  const child = new FakeChild();
  const { session, spawns } = sessionOver([child]);
  const first = session.open(CONTEXT, () => {});
  await child.say(ANNOUNCEMENT);
  await first;

  const second = await session.open(CONTEXT, () => {});

  assert.deepEqual(second, { kind: "serving", url: "http://127.0.0.1:43773/", reused: true });
  assert.equal(spawns.length, 1, "a second `swcraft docs` must not be started");
});

test("open: a second invocation during the start shares it and spawns nothing", async () => {
  // Two guide links clicked before the first child announced: a second
  // `swcraft docs` would announce a second port, and a second port is a second
  // guide tab, because the adapter reuses the tab by its address.
  const child = new FakeChild();
  const { session, spawns } = sessionOver([child]);
  const first = session.open(CONTEXT, () => {});
  const second = session.open(CONTEXT, () => {});
  await child.say(ANNOUNCEMENT);

  assert.deepEqual(await first, { kind: "serving", url: "http://127.0.0.1:43773/", reused: false });
  assert.deepEqual(await second, { kind: "serving", url: "http://127.0.0.1:43773/", reused: true });
  assert.equal(spawns.length, 1, "a start in flight must be shared, not repeated");
});

test("open: a failed start is shared by its waiters and then forgotten", async () => {
  const refused = new FakeChild();
  const fresh = new FakeChild();
  const { session, spawns } = sessionOver([refused, fresh]);
  const first = session.open(CONTEXT, () => {});
  const second = session.open(CONTEXT, () => {});
  await refused.complain(REFUSAL);
  refused.emit("close", 1);

  const starter = await first;
  const waiter = await second;
  assert.equal(starter.kind, "refused");
  assert.equal(waiter.kind, "refused");
  // Only the starter's outcome is the one to report: the waiter's is marked, so
  // two clicks during one failed start give one notification, not two.
  assert.ok(!("shared" in starter), "the starter's failure is not marked shared");
  assert.ok("shared" in waiter && waiter.shared === true, "the waiter's failure is marked shared");
  assert.equal(spawns.length, 1);

  const again = session.open(CONTEXT, () => {});
  await fresh.say("  http://127.0.0.1:9001/\n");
  assert.deepEqual(await again, { kind: "serving", url: "http://127.0.0.1:9001/", reused: false });
  assert.equal(spawns.length, 2, "the next invocation after a failure starts afresh");
});

test("open: a server that died on its own is not handed out again", async () => {
  const first = new FakeChild();
  const second = new FakeChild();
  const { session, spawns } = sessionOver([first, second]);
  const started = session.open(CONTEXT, () => {});
  await first.say(ANNOUNCEMENT);
  await started;

  // Killed from outside, or crashed: nothing is listening on that address any
  // more, so reusing it would open a dead link.
  first.emit("close", 0);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(session.url, undefined);

  const again = session.open(CONTEXT, () => {});
  await second.say("  http://127.0.0.1:9001/\n");
  assert.deepEqual(await again, { kind: "serving", url: "http://127.0.0.1:9001/", reused: false });
  assert.equal(spawns.length, 2);
});

// --- the lifecycle -----------------------------------------------------------

test("dispose: kills the running child and forgets it", async () => {
  const child = new FakeChild();
  const { session } = sessionOver([child]);
  const running = session.open(CONTEXT, () => {});
  await child.say(ANNOUNCEMENT);
  await running;

  session.dispose();

  assert.equal(child.killed, 1, "the spawned server must be killed on deactivate");
  assert.equal(session.url, undefined);
});

test("dispose: is safe when nothing is running, and does not kill twice", async () => {
  const idle = sessionOver([]);
  idle.session.dispose();
  idle.session.dispose();
  assert.equal(idle.spawns.length, 0);

  const child = new FakeChild();
  const { session } = sessionOver([child]);
  const running = session.open(CONTEXT, () => {});
  await child.say(ANNOUNCEMENT);
  await running;
  session.dispose();
  session.dispose();
  assert.equal(child.killed, 1);
});

test("dispose: during a start kills the child, and neither the starter nor a waiter is served", async () => {
  // Between the spawn and the announcement the child is not yet `running`; a
  // dispose there that killed nothing would leave a live server, its port
  // bound, on a session nobody disposes again.
  const child = new FakeChild();
  const { session, spawns } = sessionOver([child]);
  const first = session.open(CONTEXT, () => {});
  const second = session.open(CONTEXT, () => {});

  session.dispose();
  assert.equal(child.killed, 1, "the child in flight must be killed on deactivate");

  // The announcement that was already on its way still lands.
  await child.say(ANNOUNCEMENT);
  assert.deepEqual(await first, { kind: "stopped" });
  assert.deepEqual(await second, { kind: "stopped" });
  assert.equal(session.url, undefined, "a disposed session hands out no address");
  assert.equal(child.killed, 1, "killed once, not again on the late announcement");
  assert.equal(spawns.length, 1);
});

test("dispose: a start whose child exits from the kill answers stopped, not a refusal", async () => {
  const child = new FakeChild();
  const { session } = sessionOver([child]);
  const running = session.open(CONTEXT, () => {});
  session.dispose();
  child.emit("close", null);
  assert.deepEqual(await running, { kind: "stopped" });
});

test("open: after dispose answers stopped and spawns nothing", async () => {
  // A `Learn more` press resolving after deactivation must not start a server
  // nothing will ever kill.
  const { session, spawns } = sessionOver([]);
  session.dispose();
  assert.deepEqual(await session.open(CONTEXT, () => {}), { kind: "stopped" });
  assert.equal(spawns.length, 0);
});

// --- the failure paths -------------------------------------------------------

test("open: a CLI that exits before announcing is the refusal, with its stderr carried whole", async () => {
  // The "no payload installed" path. The refusal names the directory it looked
  // in and the release archive, and both must reach the adapter intact — a
  // message that only said "the CLI failed" would strand the reader.
  const child = new FakeChild();
  const { session, spawns } = sessionOver([child]);
  const logged: string[] = [];
  const running = session.open(CONTEXT, (text) => logged.push(text));
  await child.complain(REFUSAL);
  child.emit("close", 1);
  const outcome = await running;

  assert.ok(outcome.kind === "refused", `expected a refusal, got ${outcome.kind}`);
  assert.equal(outcome.exitCode, 1);
  assert.ok(outcome.detail.includes("swcraft_tools/guide_site"));
  assert.ok(outcome.detail.includes("swcraft-guide-0.5.1.zip"));
  assert.ok(logged.join("").includes("carries no user guide to serve"));
  assert.equal(session.url, undefined, "a refused run leaves nothing to reuse");
  assert.equal(spawns.length, 1);
});

test("open: the refusal notification carries the headline and points at the channel", () => {
  const message = guideRefusedMessage(1, REFUSAL);
  assert.ok(message.includes("exit code 1"));
  assert.ok(message.includes("This copy of SWCraft carries no user guide to serve."));
  // The guide's own channel, by name: the shared `SWCraft` channel is where the
  // reader would look for it otherwise, and it is not there.
  assert.ok(message.includes(`${GUIDE_OUTPUT_CHANNEL_NAME} output channel`));
  // Newlines are collapsed by a VS Code notification, so the multi-line body
  // stays in the channel rather than being pasted into one unreadable line.
  assert.equal(message.includes("\n"), false);
});

test("open: an exit code of 0 before an address is still 'exited without announcing'", async () => {
  // `swcraft docs` does not exit on success at all, so a zero exit here says
  // the same thing a non-zero one does: no address was ever given out.
  const child = new FakeChild();
  const { session } = sessionOver([child]);
  const running = session.open(CONTEXT, () => {});
  child.emit("close", 0);
  const outcome = await running;
  assert.equal(outcome.kind, "refused");
});

test("open: a child that cannot be launched at all is the spawn failure", async () => {
  const child = new FakeChild();
  const { session } = sessionOver([child]);
  const logged: string[] = [];
  const running = session.open(CONTEXT, (text) => logged.push(text));
  child.emit("error", new Error("spawn /w/.swcraft/current/bin/swcraft ENOENT"));
  const outcome = await running;

  assert.ok(outcome.kind === "spawn-failed", `expected a spawn failure, got ${outcome.kind}`);
  assert.equal(outcome.message, guideSpawnFailedMessage(CONTEXT.executable));
  assert.ok(outcome.message.includes("/w/.swcraft/current/bin/swcraft"));
  assert.ok(outcome.message.includes("Check Editor Tooling"));
  assert.ok(logged.join("").includes("ENOENT"));
  assert.equal(session.url, undefined);
});

test("open: a child that neither announces nor exits is killed and reported", async () => {
  const child = new FakeChild();
  const { session } = sessionOver([child], 20);
  const outcome = await session.open(CONTEXT, () => {});

  assert.deepEqual(outcome, { kind: "no-announcement", message: guideNoAnnouncementMessage(20) });
  assert.equal(child.killed, 1, "a child that said nothing must not be left holding a socket");
  assert.equal(session.url, undefined);
});

test("open: an address arriving after the timeout does not resurrect the run", async () => {
  const child = new FakeChild();
  const { session } = sessionOver([child], 20);
  const outcome = await session.open(CONTEXT, () => {});
  assert.equal(outcome.kind, "no-announcement");

  await child.say(ANNOUNCEMENT);
  assert.equal(session.url, undefined);
});

test("the announcement timeout is generous and is a named constant", () => {
  // Not a performance assertion (the measured announcement took 0.37 s): the bound makes "the
  // CLI started and then said nothing" a failure with a message, not a command that never
  // returns.
  assert.equal(GUIDE_ANNOUNCE_TIMEOUT_MS, 20_000);
  assert.ok(guideNoAnnouncementMessage(GUIDE_ANNOUNCE_TIMEOUT_MS).includes("20 seconds"));
});

// --- the guide's own output channel -----------------------------------------
//
// `swcraft docs` logs one HTTP request per line for as long as the server is up; in the
// shared `SWCraft` channel every later command's answer would arrive buried in it. These
// cases see this module's half: everything one run produces goes to the channel it was
// handed, and only a failure reveals it. The other half — that the channel handed in is the
// guide's — lives in `commands.ts` and is pinned by `commandCore.test.ts`'s source scan.

test("the guide channel is named, and is not the shared SWCraft channel", () => {
  assert.equal(GUIDE_OUTPUT_CHANNEL_NAME, "SWCraft Guide");
  assert.notEqual(GUIDE_OUTPUT_CHANNEL_NAME, "SWCraft");
  // Both failure notifications send the reader to it by name; neither may keep
  // pointing at the channel the text is no longer in.
  for (const message of [guideRefusedMessage(1, REFUSAL), guideNoAnnouncementMessage(20_000)]) {
    assert.ok(
      message.includes(`${GUIDE_OUTPUT_CHANNEL_NAME} output channel`),
      `must name the guide channel: ${message}`,
    );
  }
});

test("serveGuideTo: the command line, the announcement and the request log all go to one channel", async () => {
  const guideChannel = new RecordingChannel();
  // Stands in for the shared `SWCraft` channel every other command writes to.
  // Nothing this run produces may reach it.
  const sharedChannel = new RecordingChannel();
  const child = new FakeChild();
  const { session } = sessionOver([child]);

  const running = serveGuideTo(session, CONTEXT, guideChannel);
  await child.say(ANNOUNCEMENT);
  const outcome = await running;
  // The server keeps logging long after the address was handed out: the traffic that would
  // bury the next command's answer in a shared channel.
  await child.complain(REQUEST_LOG);

  assert.equal(outcome.kind, "serving");
  assert.ok(guideChannel.text.includes("$ swcraft docs"));
  assert.ok(guideChannel.text.includes("Press Ctrl+C to stop."));
  assert.ok(guideChannel.text.includes('"GET /reference/model/interfaces/ HTTP/1.1" 200 -'));
  assert.deepEqual(sharedChannel.appended, [], "no guide output may reach the shared channel");
  assert.equal(sharedChannel.shown, 0);
});

test("serveGuideTo: a successful run reveals no channel at all", async () => {
  // The happy path ends in the guide's page; popping a panel open behind it is
  // noise, and popping the SHARED one open would be noise in the wrong place.
  const guideChannel = new RecordingChannel();
  const child = new FakeChild();
  const { session } = sessionOver([child]);

  const running = serveGuideTo(session, CONTEXT, guideChannel);
  await child.say(ANNOUNCEMENT);
  await running;
  await child.complain(REQUEST_LOG);

  assert.equal(guideChannel.shown, 0, "a served guide must not pop the output panel open");
});

test("serveGuideTo: a refusal shows the guide channel, with the whole refusal in it", async () => {
  // The no-payload refusal names the directory that was searched and the
  // release archive to unpack. A VS Code notification cannot carry five lines,
  // so the channel has to hold them AND be the one that comes to the front.
  const guideChannel = new RecordingChannel();
  const sharedChannel = new RecordingChannel();
  const child = new FakeChild();
  const { session } = sessionOver([child]);

  const running = serveGuideTo(session, CONTEXT, guideChannel);
  await child.complain(REFUSAL);
  child.emit("close", 1);
  const outcome = await running;

  assert.equal(outcome.kind, "refused");
  assert.equal(guideChannel.shown, 1, "the refusal must be brought to the front");
  assert.equal(guideChannel.lastPreserveFocus, true, "revealed without stealing focus");
  assert.ok(guideChannel.text.includes("swcraft_tools/guide_site"));
  assert.ok(guideChannel.text.includes("swcraft-guide-0.5.1.zip"));
  assert.equal(sharedChannel.shown, 0, "the shared channel holds none of this and is not shown");
  assert.deepEqual(sharedChannel.appended, []);
});

test("serveGuideTo: the other two failures show the guide channel too", async () => {
  // `spawn-failed` and `no-announcement`: both notifications point at the guide
  // channel, so both must reveal it — a message naming a panel the user has to
  // find by hand is the failure this rule prevents.
  const spawnFailure = new RecordingChannel();
  const first = new FakeChild();
  const { session: spawnSession } = sessionOver([first]);
  const spawnRun = serveGuideTo(spawnSession, CONTEXT, spawnFailure);
  first.emit("error", new Error("spawn /w/.swcraft/current/bin/swcraft ENOENT"));
  assert.equal((await spawnRun).kind, "spawn-failed");
  assert.equal(spawnFailure.shown, 1);
  assert.ok(spawnFailure.text.includes("ENOENT"));

  const silence = new RecordingChannel();
  const second = new FakeChild();
  const { session: quietSession } = sessionOver([second], 20);
  assert.equal((await serveGuideTo(quietSession, CONTEXT, silence)).kind, "no-announcement");
  assert.equal(silence.shown, 1);
});

test("serveGuideTo: a waiter on a failed start and a disposed session reveal nothing", async () => {
  // The starter reveals the channel once; a second click during the same failed
  // start must not reveal it again, and a disposed session has no window to
  // reveal it in.
  const channel = new RecordingChannel();
  const child = new FakeChild();
  const { session } = sessionOver([child]);
  const starter = serveGuideTo(session, CONTEXT, channel);
  const waiter = serveGuideTo(session, CONTEXT, channel);
  await child.complain(REFUSAL);
  child.emit("close", 1);
  assert.equal((await starter).kind, "refused");
  assert.equal((await waiter).kind, "refused");
  assert.equal(channel.shown, 1, "only the starter reveals the channel");

  const disposedChannel = new RecordingChannel();
  const { session: disposed } = sessionOver([]);
  disposed.dispose();
  assert.equal((await serveGuideTo(disposed, CONTEXT, disposedChannel)).kind, "stopped");
  assert.equal(disposedChannel.shown, 0);
});

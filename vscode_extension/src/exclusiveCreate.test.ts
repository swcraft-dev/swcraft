// Unit tests for `writeFileExclusive()`: an exclusive create that refuses any entry at the
// name, a dangling symbolic link included, on Windows as on POSIX. The link cases skip where
// a link cannot be made (Windows without the privilege), except under
// `SWCRAFT_REQUIRE_SYMLINKS=1`, which both CI jobs set: there they fail, so a silent skip
// cannot pass for the Windows proof. The probe is a real attempt, never a platform check.

import assert from "node:assert/strict";
import nodeFs, * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { type TestContext, test } from "node:test";

import {
  LINK_NOT_SUPPORTED_CODES,
  type LinkFile,
  TEMPORARY_ATTEMPTS,
  type WriteData,
  writeFileExclusive,
} from "./exclusiveCreate";

function tempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "swcraft-vscode-exclusive-create-test-"));
}

/** The names in *dir*, sorted: a temporary file left behind shows here. */
function entries(dir: string): string[] {
  return fs.readdirSync(dir).sort();
}

/**
 * A file symbolic link at *link* -> *target*, else the test skipped — or, under
 * `SWCRAFT_REQUIRE_SYMLINKS=1`, failed.
 */
function fileLinkOrSkip(t: TestContext, target: string, link: string): boolean {
  try {
    fs.symlinkSync(target, link, "file");
    return true;
  } catch (error) {
    if (process.env.SWCRAFT_REQUIRE_SYMLINKS === "1") {
      assert.fail(
        `symlinks are not available here: ${String(error)} (SWCRAFT_REQUIRE_SYMLINKS=1 requires them)`,
      );
    }
    t.skip(`symlinks are not available here: ${String(error)}`);
    return false;
  }
}

/** Whether this filesystem honours a create's mode bits (a probe, not a platform check). */
function modesHonoured(dir: string): boolean {
  const probe = path.join(dir, "mode-probe");
  fs.writeFileSync(probe, "", { mode: 0o600 });
  const honoured = (fs.statSync(probe).mode & 0o777) === 0o600;
  fs.rmSync(probe);
  return honoured;
}

/** A link call failing with *code*, as a filesystem without hard links does. */
function failingLink(code: string): LinkFile {
  return () => {
    throw Object.assign(new Error(`${code}: injected`), { code });
  };
}

function assertEexist(action: () => void): void {
  assert.throws(action, (error: NodeJS.ErrnoException) => error.code === "EEXIST");
}

test("writeFileExclusive: a new file is written whole, with its mode, and nothing else is left", () => {
  const dir = tempDir();
  try {
    const dest = path.join(dir, "new.txt");
    writeFileExclusive(dest, "line one\nline two\n", { mode: 0o600, encoding: "utf8" });
    assert.equal(fs.readFileSync(dest, "utf8"), "line one\nline two\n");
    assert.deepEqual(entries(dir), ["new.txt"]);
    if (modesHonoured(dir)) {
      assert.equal(fs.statSync(dest).mode & 0o777, 0o600);
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeFileExclusive: a descriptor's identity that disagrees with the name's still removes the temporary file", (t) => {
  // On Windows, libuv's fstat of a descriptor and its lstat of a path can disagree on one file,
  // which left every temporary file behind. A disagreeing fstat stands in for it here on every
  // platform: the identity is the name's, so the cleanup still matches.
  const dir = tempDir();
  try {
    const real = nodeFs.fstatSync;
    t.mock.method(nodeFs, "fstatSync", (...args: Parameters<typeof nodeFs.fstatSync>) => {
      const stats = real(...args);
      if (typeof stats === "object" && stats !== null && typeof stats.dev === "bigint") {
        return Object.assign(Object.create(Object.getPrototypeOf(stats)), stats, {
          dev: stats.dev + 1n,
        });
      }
      return stats;
    });
    const dest = path.join(dir, "new.txt");
    writeFileExclusive(dest, "body");
    t.mock.restoreAll();
    assert.equal(fs.readFileSync(dest, "utf8"), "body");
    assert.deepEqual(entries(dir), ["new.txt"]);
  } finally {
    t.mock.restoreAll();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeFileExclusive: an existing file is refused EEXIST and left byte-equal", () => {
  const dir = tempDir();
  try {
    const dest = path.join(dir, "taken.txt");
    fs.writeFileSync(dest, "theirs");
    assertEexist(() => writeFileExclusive(dest, Buffer.from("ours")));
    assert.equal(fs.readFileSync(dest, "utf8"), "theirs");
    assert.deepEqual(entries(dir), ["taken.txt"]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeFileExclusive: a dangling symbolic link is refused EEXIST and its target is not created", (t) => {
  const dir = tempDir();
  try {
    const target = path.join(dir, "target.txt");
    const dest = path.join(dir, "link.txt");
    if (!fileLinkOrSkip(t, target, dest)) {
      return;
    }
    assertEexist(() => writeFileExclusive(dest, "ours"));
    assert.equal(fs.existsSync(target), false, "the link's target was created");
    assert.ok(fs.lstatSync(dest).isSymbolicLink());
    assert.deepEqual(entries(dir), ["link.txt"]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeFileExclusive: a link to a file is refused EEXIST and the file is untouched", (t) => {
  const dir = tempDir();
  try {
    const target = path.join(dir, "target.txt");
    fs.writeFileSync(target, "theirs");
    const dest = path.join(dir, "link.txt");
    if (!fileLinkOrSkip(t, target, dest)) {
      return;
    }
    assertEexist(() => writeFileExclusive(dest, "ours"));
    assert.equal(fs.readFileSync(target, "utf8"), "theirs");
    assert.ok(fs.lstatSync(dest).isSymbolicLink());
    assert.deepEqual(entries(dir), ["link.txt", "target.txt"]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeFileExclusive: a link that is not supported falls back to the exclusive write", () => {
  for (const code of LINK_NOT_SUPPORTED_CODES) {
    const dir = tempDir();
    try {
      const dest = path.join(dir, "new.txt");
      writeFileExclusive(dest, "ours", {}, { link: failingLink(code) });
      assert.equal(fs.readFileSync(dest, "utf8"), "ours", code);
      assert.deepEqual(entries(dir), ["new.txt"], code);
      // The fallback is still exclusive: an existing file is refused EEXIST there too.
      assertEexist(() => writeFileExclusive(dest, "again", {}, { link: failingLink(code) }));
      assert.equal(fs.readFileSync(dest, "utf8"), "ours", code);
      assert.deepEqual(entries(dir), ["new.txt"], code);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }
});

test("writeFileExclusive: any other link failure is raised, nothing is created and the temporary file goes", () => {
  const dir = tempDir();
  try {
    const dest = path.join(dir, "new.txt");
    assert.throws(
      () => writeFileExclusive(dest, "ours", {}, { link: failingLink("EACCES") }),
      (error: NodeJS.ErrnoException) => error.code === "EACCES",
    );
    assert.deepEqual(entries(dir), []);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeFileExclusive: the temporary file is a hidden sibling named for the destination", () => {
  const dir = tempDir();
  try {
    const dest = path.join(dir, "new.txt");
    const seen: string[] = [];
    writeFileExclusive(
      dest,
      "ours",
      {},
      {
        link: (existingPath, newPath) => {
          seen.push(existingPath);
          fs.linkSync(existingPath, newPath);
        },
      },
    );
    assert.equal(seen.length, 1);
    assert.equal(path.dirname(seen[0]), dir);
    assert.match(
      path.basename(seen[0]),
      new RegExp(`^\\.new\\.txt\\.swcraft-${process.pid}-[0-9a-f]{12}\\.tmp$`),
    );
    assert.deepEqual(entries(dir), ["new.txt"]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

/** A write that fails with *code* after the create, on the calls *failing* counts (1-based). */
function failingWrite(code: string, failing: ReadonlySet<number>): WriteData {
  let calls = 0;
  return (descriptor, data, encoding) => {
    calls += 1;
    if (failing.has(calls)) {
      fs.writeSync(descriptor, "half");
      throw Object.assign(new Error(`${code}: injected`), { code });
    }
    fs.writeFileSync(descriptor, data, encoding === undefined ? {} : { encoding });
  };
}

/** A suffix sequence: *names* in order, then the last one again. */
function suffixes(...names: string[]): () => string {
  let next = 0;
  return () => names[Math.min(next++, names.length - 1)];
}

function temporaryName(dest: string, suffix: string): string {
  return `.${path.basename(dest)}.swcraft-${process.pid}-${suffix}.tmp`;
}

test("writeFileExclusive: a foreign entry at the temporary name is kept and a fresh name is tried", () => {
  const dir = tempDir();
  try {
    const dest = path.join(dir, "new.txt");
    const foreign = path.join(dir, temporaryName(dest, "aaaaaaaaaaaa"));
    fs.writeFileSync(foreign, "theirs");
    writeFileExclusive(
      dest,
      "ours",
      {},
      { randomSuffix: suffixes("aaaaaaaaaaaa", "bbbbbbbbbbbb") },
    );
    assert.equal(fs.readFileSync(dest, "utf8"), "ours");
    assert.equal(fs.readFileSync(foreign, "utf8"), "theirs");
    assert.deepEqual(entries(dir), [path.basename(foreign), "new.txt"].sort());
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeFileExclusive: after the bounded attempts the temporary name's EEXIST is raised and the foreign file survives", () => {
  const dir = tempDir();
  try {
    const dest = path.join(dir, "new.txt");
    const foreign = path.join(dir, temporaryName(dest, "aaaaaaaaaaaa"));
    fs.writeFileSync(foreign, "theirs");
    let asked = 0;
    assertEexist(() =>
      writeFileExclusive(
        dest,
        "ours",
        {},
        {
          randomSuffix: () => {
            asked += 1;
            return "aaaaaaaaaaaa";
          },
        },
      ),
    );
    assert.equal(asked, TEMPORARY_ATTEMPTS);
    assert.equal(fs.readFileSync(foreign, "utf8"), "theirs");
    assert.deepEqual(entries(dir), [path.basename(foreign)]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeFileExclusive: a failure after the temporary file's create removes it and creates nothing", () => {
  const dir = tempDir();
  try {
    const dest = path.join(dir, "new.txt");
    assert.throws(
      () =>
        writeFileExclusive(dest, "ours", {}, { writeData: failingWrite("ENOSPC", new Set([1])) }),
      (error: NodeJS.ErrnoException) => error.code === "ENOSPC",
    );
    assert.deepEqual(entries(dir), []);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeFileExclusive: a failure after the fallback's create removes the half-written destination", () => {
  const dir = tempDir();
  try {
    const dest = path.join(dir, "new.txt");
    assert.throws(
      () =>
        writeFileExclusive(
          dest,
          "ours",
          {},
          { link: failingLink("ENOTSUP"), writeData: failingWrite("ENOSPC", new Set([2])) },
        ),
      (error: NodeJS.ErrnoException) => error.code === "ENOSPC",
    );
    assert.deepEqual(entries(dir), []);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeFileExclusive: on the fallback a dangling link at the name is refused EEXIST before any write", (t) => {
  const dir = tempDir();
  try {
    const target = path.join(dir, "target.txt");
    const dest = path.join(dir, "link.txt");
    if (!fileLinkOrSkip(t, target, dest)) {
      return;
    }
    for (const code of LINK_NOT_SUPPORTED_CODES) {
      assertEexist(() => writeFileExclusive(dest, "ours", {}, { link: failingLink(code) }));
      assert.equal(fs.existsSync(target), false, `${code}: the link's target was created`);
      assert.ok(fs.lstatSync(dest).isSymbolicLink(), code);
      assert.deepEqual(entries(dir), ["link.txt"], code);
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

/**
 * A write that, on the calls *failing* counts (1-based), moves the file it is
 * writing aside (an open file can be renamed on Windows too: libuv opens with
 * `FILE_SHARE_DELETE`), puts another writer's file at *name* and then fails —
 * a replacement landing between this call's create and its cleanup.
 */
function replacingWrite(name: () => string, failing: ReadonlySet<number>): WriteData {
  let calls = 0;
  return (descriptor, data, encoding) => {
    calls += 1;
    if (failing.has(calls)) {
      fs.renameSync(name(), `${name()}.moved-aside`);
      fs.writeFileSync(name(), "theirs");
      throw Object.assign(new Error("ENOSPC: injected"), { code: "ENOSPC" });
    }
    fs.writeFileSync(descriptor, data, encoding === undefined ? {} : { encoding });
  };
}

test("writeFileExclusive: another writer's replacement of the temporary file before a failed write survives", () => {
  const dir = tempDir();
  try {
    const dest = path.join(dir, "new.txt");
    const temporary = path.join(dir, temporaryName(dest, "aaaaaaaaaaaa"));
    assert.throws(
      () =>
        writeFileExclusive(
          dest,
          "ours",
          {},
          {
            randomSuffix: () => "aaaaaaaaaaaa",
            writeData: replacingWrite(() => temporary, new Set([1])),
          },
        ),
      (error: NodeJS.ErrnoException) => error.code === "ENOSPC",
    );
    assert.equal(fs.readFileSync(temporary, "utf8"), "theirs");
    assert.deepEqual(
      entries(dir),
      [path.basename(temporary), `${path.basename(temporary)}.moved-aside`].sort(),
    );
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeFileExclusive: another writer's replacement of the temporary file before a failed link survives", () => {
  const dir = tempDir();
  try {
    const dest = path.join(dir, "new.txt");
    const temporary = path.join(dir, temporaryName(dest, "aaaaaaaaaaaa"));
    assert.throws(
      () =>
        writeFileExclusive(
          dest,
          "ours",
          {},
          {
            randomSuffix: () => "aaaaaaaaaaaa",
            link: (existingPath) => {
              fs.renameSync(existingPath, `${existingPath}.moved-aside`);
              fs.writeFileSync(existingPath, "theirs");
              throw Object.assign(new Error("EACCES: injected"), { code: "EACCES" });
            },
          },
        ),
      (error: NodeJS.ErrnoException) => error.code === "EACCES",
    );
    assert.equal(fs.readFileSync(temporary, "utf8"), "theirs");
    assert.deepEqual(
      entries(dir),
      [path.basename(temporary), `${path.basename(temporary)}.moved-aside`].sort(),
    );
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeFileExclusive: another writer's replacement of the fallback's destination before a failed write survives", () => {
  const dir = tempDir();
  try {
    const dest = path.join(dir, "new.txt");
    assert.throws(
      () =>
        writeFileExclusive(
          dest,
          "ours",
          {},
          { link: failingLink("ENOTSUP"), writeData: replacingWrite(() => dest, new Set([2])) },
        ),
      (error: NodeJS.ErrnoException) => error.code === "ENOSPC",
    );
    assert.equal(fs.readFileSync(dest, "utf8"), "theirs");
    // The temporary file, this call's own and still at its name, goes.
    assert.deepEqual(entries(dir), ["new.txt", "new.txt.moved-aside"]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

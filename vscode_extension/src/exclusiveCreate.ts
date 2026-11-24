// An exclusive create that follows no link at the name, on Windows too.
// No `vscode` import: it runs under plain `node --test`.

import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

/** What `writeFileExclusive()` passes to the temporary file's write. */
export interface ExclusiveWriteOptions {
  mode?: fs.Mode;
  encoding?: BufferEncoding;
}

/** The hard-link call `writeFileExclusive()` makes: `fs.linkSync`, or a test's stand-in. */
export type LinkFile = (existingPath: string, newPath: string) => void;

/** The write through a descriptor this call created: the whole of *data*, or a test's failing stand-in. */
export type WriteData = (
  descriptor: number,
  data: string | NodeJS.ArrayBufferView,
  encoding: BufferEncoding | undefined,
) => void;

/** The test seams of `writeFileExclusive()`; production passes none. */
export interface ExclusiveSeams {
  /** The hard-link call, `fs.linkSync` unless a test injects a failing one. */
  link?: LinkFile;
  /** The random part of the temporary file's name, twelve hex digits unless a test fixes it. */
  randomSuffix?: () => string;
  /** The data's write through the new descriptor, `fs.writeFileSync(descriptor, …)` unless a test fails it. */
  writeData?: WriteData;
}

/** How many random names `writeFileExclusive()` tries for its temporary file before it raises. */
export const TEMPORARY_ATTEMPTS = 8;

/**
 * The `linkSync` error codes that say the filesystem makes no hard link here; on them
 * `writeFileExclusive()` falls back to a direct exclusive write **when no entry is at the name**:
 * `ENOTSUP`/`EOPNOTSUPP` (FAT and exFAT; Windows `ERROR_NOT_SUPPORTED`), `EPERM` (Linux vfat;
 * Windows `ERROR_ACCESS_DENIED`, also a permission or a pending delete), `ENOSYS`, `EINVAL`
 * (Windows `ERROR_INVALID_PARAMETER`, some network redirectors) and `EISDIR` (libuv's spelling of
 * `ERROR_INVALID_FUNCTION`, which `CreateHardLinkW` returns on FAT). `EPERM` and `EINVAL` are
 * broad, so the fallback first refuses with `EEXIST` when `lstat` finds anything at the name.
 * `EXDEV` is not one (the temporary file is a sibling); `EEXIST` is the refusal itself.
 */
export const LINK_NOT_SUPPORTED_CODES: ReadonlySet<string> = new Set([
  "ENOTSUP",
  "EOPNOTSUPP",
  "EPERM",
  "ENOSYS",
  "EINVAL",
  "EISDIR",
]);

const defaultRandomSuffix = (): string => crypto.randomBytes(6).toString("hex");

const defaultWriteData: WriteData = (descriptor, data, encoding) => {
  fs.writeFileSync(descriptor, data, encoding === undefined ? {} : { encoding });
};

/** What identifies a file this call created: its device and inode, exact (`bigint`). */
interface CreatedIdentity {
  dev: bigint;
  ino: bigint;
}

/**
 * Remove *file* only while the name is still the file *created* identifies (`lstat`, same `dev`
 * and `ino`, as `bigint` so a 64-bit Windows file index compares exactly). Both readings are
 * `lstat`s of the name: on Windows libuv's `fstat` of a descriptor and `lstat` of a path disagree
 * on the same file. Another entry at the name stays; a failing `lstat` or removal is suppressed
 * so it never masks the error that caused the cleanup. The window between the `lstat` and the
 * removal is the module's stated limit.
 */
function removeIfCreated(file: string, created: CreatedIdentity | undefined): void {
  if (created === undefined) {
    return; // unidentified: the file stays rather than a guess removing another writer's
  }
  try {
    const now = fs.lstatSync(file, { bigint: true, throwIfNoEntry: false });
    if (now !== undefined && now.dev === created.dev && now.ino === created.ino) {
      fs.rmSync(file, { force: true });
    }
  } catch {
    // never mask the error that caused the cleanup
  }
}

/**
 * Create *file* exclusively (`wx`, *options*' mode), record its identity (an `lstat` right after
 * the create) and write *data* through that descriptor. A failure after the create removes the
 * file while the name is still that file (`removeIfCreated`); a failure of the create itself
 * removes nothing. An entry swapped in between the create and the `lstat` would be taken for this
 * call's, the same non-atomic window as the removal's. Returns the identity for the caller's own
 * cleanup.
 */
function createAndWrite(
  file: string,
  data: string | NodeJS.ArrayBufferView,
  options: ExclusiveWriteOptions,
  writeData: WriteData,
): CreatedIdentity {
  const descriptor = fs.openSync(file, "wx", options.mode);
  let created: CreatedIdentity | undefined;
  try {
    try {
      const stats = fs.lstatSync(file, { bigint: true });
      created = { dev: stats.dev, ino: stats.ino };
      writeData(descriptor, data, options.encoding);
      return created;
    } finally {
      fs.closeSync(descriptor); // a failing close reaches the cleanup below
    }
  } catch (error) {
    removeIfCreated(file, created);
    throw error;
  }
}

/** The `EEXIST` a `wx` open of *dest* raises, for an entry `lstat` found at the name. */
function existsError(dest: string): NodeJS.ErrnoException {
  return Object.assign(new Error(`EEXIST: file already exists, open '${dest}'`), {
    code: "EEXIST",
    errno: -os.constants.errno.EEXIST,
    syscall: "open",
    path: dest,
  });
}

/**
 * Create *dest* with *data*, refusing with `EEXIST` any entry already at the name (a dangling
 * symbolic link included) and following no link there, on Windows as on POSIX.
 *
 * A `wx` write is `O_CREAT | O_EXCL` on POSIX and never follows a link; on Windows it is
 * `CreateFile` with `CREATE_NEW`, which follows a dangling link and creates its target, and libuv
 * has no flag to open the reparse point instead. So the data goes to a sibling temporary file
 * `.<name>.swcraft-<pid>-<random>.tmp` (`wx`, *options*' mode), which is then **hard-linked** to
 * *dest*: a hard link is refused with `EEXIST` by any entry at the name on Windows and POSIX
 * alike. The temporary file is removed whenever the name is still the file this call created.
 * *dest* appears whole, never half written. An `EEXIST` on the temporary file's create is retried
 * under a fresh name, up to `TEMPORARY_ATTEMPTS`; the last one is raised and reads as *dest* taken. *seams* are for tests.
 *
 * What this does not reach: without hard links (FAT, exFAT, some network shares) the write falls
 * back, on `LINK_NOT_SUPPORTED_CODES`, to an `lstat` of *dest* (refusing with `EEXIST` when any
 * entry is there) and a direct `wx` write; a link planted between that `lstat` and the open is
 * followed on Windows. Every removal of a file this call created is by identity and not atomic: a
 * replacement landing between the identity `lstat` and the removal is removed (POSIX has no
 * unlink-if-inode; Node has no delete through a descriptor); a failing `lstat` leaves the file in
 * place. A link planted at the temporary file's random name is followed on Windows. `O_EXCL` stays
 * the filesystem's guarantee (NFS before version 3). A junction or link on the way **to** *dest*,
 * and a folder swapped for a link after a caller's own checks, are followed. Errors carry Node's
 * message, which names the paths.
 */
export function writeFileExclusive(
  dest: string,
  data: string | NodeJS.ArrayBufferView,
  options: ExclusiveWriteOptions = {},
  seams: ExclusiveSeams = {},
): void {
  const link = seams.link ?? fs.linkSync;
  const randomSuffix = seams.randomSuffix ?? defaultRandomSuffix;
  const writeData = seams.writeData ?? defaultWriteData;
  let temporary = "";
  let temporaryIdentity: CreatedIdentity | undefined;
  for (let attempt = 1; ; attempt += 1) {
    temporary = path.join(
      path.dirname(dest),
      `.${path.basename(dest)}.swcraft-${process.pid}-${randomSuffix()}.tmp`,
    );
    try {
      temporaryIdentity = createAndWrite(temporary, data, options, writeData);
      break;
    } catch (error) {
      // `EEXIST`: the entry at the random name is not this call's; try another name.
      if ((error as NodeJS.ErrnoException).code !== "EEXIST" || attempt === TEMPORARY_ATTEMPTS) {
        throw error;
      }
    }
  }
  try {
    try {
      link(temporary, dest);
    } catch (error) {
      if (!LINK_NOT_SUPPORTED_CODES.has((error as NodeJS.ErrnoException).code ?? "")) {
        throw error;
      }
      // No hard link here: only an absent name takes the direct write.
      if (fs.lstatSync(dest, { throwIfNoEntry: false }) !== undefined) {
        throw existsError(dest);
      }
      createAndWrite(dest, data, options, writeData);
    }
  } finally {
    removeIfCreated(temporary, temporaryIdentity);
  }
}

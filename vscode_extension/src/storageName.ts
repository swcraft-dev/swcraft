// Storage names and the output channel's quoting: the leaf both the diagrams' tab
// (`diagramsTab.ts`) and the URI handler's model route (`helpLinks.ts`) check what crosses into
// the editor with, so neither module reaches into the other.
//
// No `vscode` import: it runs under plain `node --test`. `checkStorageName()` and `channelQuote()`
// are pure; the resolvers read only real paths and a `stat`, never a file's content.

import * as fs from "node:fs";
import * as path from "node:path";

/** Why a request is not followed: one line for the output channel. */
export type Refused = { kind: "refused"; reason: string };

/** The longest storage name the tab takes. */
export const STORAGE_NAME_LIMIT = 1024;

/**
 * The lexical check of a storage name: relative, POSIX, no empty, `.` or `..` segment, no
 * backslash, colon, NUL or other control character, ending `.yaml`. Answers the reason it fails,
 * or `undefined`. A drive (`C:`), a scheme and a UNC name (`//server`) fail by their colon or
 * their leading slash.
 */
export function checkStorageName(name: string): string | undefined {
  if (name.length === 0 || name.length > STORAGE_NAME_LIMIT) {
    return "the name is empty or too long";
  }
  if (name.startsWith("/")) {
    return "the name is absolute";
  }
  // biome-ignore lint/suspicious/noControlCharactersInRegex: control characters are what is refused
  if (/[\\:\u0000-\u001f\u007f]/.test(name)) {
    return "the name holds a backslash, a colon or a control character";
  }
  if (name.split("/").some((segment) => segment === "" || segment === "." || segment === "..")) {
    return "the name has an empty, '.' or '..' segment";
  }
  if (!name.endsWith(".yaml")) {
    return "the name is not a .yaml file";
  }
  return undefined;
}

/** Is *child* (a real path) strictly inside *root* (a real path)? */
function isInside(root: string, child: string): boolean {
  const relative = path.relative(root, child);
  return relative !== "" && !relative.startsWith("..") && !path.isAbsolute(relative);
}

/**
 * The real path of `<modelRoot>/<name>` when *name* passes `checkStorageName()`
 * and the file is a regular file whose real path lies under the real *modelRoot*;
 * a link to a file outside the model, a directory and a missing file are refused.
 */
export function resolveModelFile(
  modelRoot: string,
  name: string,
): { kind: "file"; path: string } | Refused {
  const lexical = checkStorageName(name);
  if (lexical !== undefined) {
    return { kind: "refused", reason: lexical };
  }
  return resolveRegularFileUnder(modelRoot, name.split("/"), "the file lies outside the model");
}

/**
 * The real path of `<root>/<segments…>` when it is a regular file whose real path lies strictly
 * inside the real *root*; a missing file, a link out (*outsideReason*), a directory or a FIFO is
 * refused.
 */
export function resolveRegularFileUnder(
  rootPath: string,
  segments: readonly string[],
  outsideReason: string,
): { kind: "file"; path: string } | Refused {
  let root: string;
  let real: string;
  try {
    root = fs.realpathSync.native(rootPath);
    real = fs.realpathSync.native(path.join(rootPath, ...segments));
  } catch {
    return { kind: "refused", reason: "the file does not exist" };
  }
  if (!isInside(root, real)) {
    return { kind: "refused", reason: outsideReason };
  }
  try {
    if (!fs.statSync(real).isFile()) {
      return { kind: "refused", reason: "the name is not a regular file" };
    }
  } catch {
    return { kind: "refused", reason: "the file does not exist" };
  }
  return { kind: "file", path: real };
}

/** Every character `channelQuote()` escapes: C0, DEL, C1 and the two Unicode line separators. */
// biome-ignore lint/suspicious/noControlCharactersInRegex: control characters are what is escaped
export const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g;

/** The longest part of a link a refusal line quotes. */
export const LINK_QUOTE_LIMIT = 200;

/**
 * *text* fit for one line of the output channel: every control character escaped as `\uXXXX`
 * **first**, then cut to *limit* characters with `…` after, so a text of control characters alone
 * is bounded too and a cut never leaves a partial escape.
 */
export function channelQuote(text: string, limit: number = LINK_QUOTE_LIMIT): string {
  const escaped = text.replace(
    CONTROL_CHARACTERS,
    (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
  if (escaped.length <= limit) {
    return escaped;
  }
  const cut = escaped.slice(0, limit).replace(/\\(?:u[0-9a-f]{0,3})?$/, "");
  return `${cut}…`;
}

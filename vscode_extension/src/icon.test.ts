// The extension's gallery icon: package.json's top-level `icon`.
//
// Declaring nothing leaves the grey placeholder in the Extensions view, a defect nothing in the
// repository notices, so the manifest's claim and the file behind it are pinned here; whether the
// file ships is `packageVsix.test.ts`'s media inventory (via `declaredMediaPaths()`).
//
// Not to be confused: `media/icon.svg` is the monochrome Activity Bar mark (`currentColor`,
// under `contributes.viewsContainers`); `media/icon-128-color.png` is the gallery tile, which
// carries its own background.
//
// The PNG is committed output, rasterised from `media/icon-128-color.svg` from `vscode_extension/`
// with `npx --yes sharp-cli@5.1.0 --input media/icon-128-color.svg
// --output media/icon-128-color.png --format png resize 128 128`. Its bytes depend on the libvips
// build behind `sharp`, so only the properties VS Code requires are pinned.
//
// No `vscode` import: this runs under plain `node --test`.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import { PROJECT_ROOT, readManifest } from "./testSupport";

/** The first eight bytes of every PNG file (RFC 2083 §3.1). */
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

/** What VS Code's gallery expects: a square 128×128 tile. */
const EXPECTED_EDGE = 128;

/** The source SVG the committed PNG is rasterised from. */
const SOURCE_SVG = "media/icon-128-color.svg";

function iconPath(): string {
  const declared = readManifest().icon;
  assert.ok(
    typeof declared === "string" && declared.length > 0,
    "package.json declares no top-level `icon`, so the Extensions view shows VS Code's placeholder",
  );
  return declared;
}

test("the manifest declares a PNG icon", () => {
  const declared = iconPath();
  // `vsce` rejects an SVG icon at package time, a failure only `dev.py package` would find.
  assert.ok(
    declared.toLowerCase().endsWith(".png"),
    `the extension icon must be a PNG (vsce rejects SVG), got: ${declared}`,
  );
  assert.ok(
    declared.startsWith("media/"),
    `the icon must live under media/ so declaredMediaPaths() carries it into the .vsix inventory check, got: ${declared}`,
  );
});

test("the declared icon is a real 128x128 PNG", () => {
  const absolute = path.join(PROJECT_ROOT, iconPath());
  assert.ok(fs.existsSync(absolute), `the declared icon does not exist: ${absolute}`);

  const bytes = fs.readFileSync(absolute);
  assert.deepEqual(
    bytes.subarray(0, PNG_SIGNATURE.length),
    PNG_SIGNATURE,
    "the declared icon does not start with the PNG signature — a renamed file is not a PNG",
  );

  // IHDR is the first chunk and its layout is fixed: an 8-byte signature, a
  // 4-byte length, the 4-byte type, then width and height as big-endian uint32.
  assert.equal(bytes.subarray(12, 16).toString("ascii"), "IHDR", "the first PNG chunk is not IHDR");
  const width = bytes.readUInt32BE(16);
  const height = bytes.readUInt32BE(20);
  assert.equal(width, EXPECTED_EDGE, `the icon is ${width}px wide, expected ${EXPECTED_EDGE}`);
  assert.equal(height, EXPECTED_EDGE, `the icon is ${height}px tall, expected ${EXPECTED_EDGE}`);
});

test("the icon's source SVG is kept beside it, and is not what the manifest points at", () => {
  // The PNG cannot be re-rendered from itself, so its source stays in the repository; the
  // manifest must keep pointing at the raster because vsce rejects a `.svg`.
  assert.ok(
    fs.existsSync(path.join(PROJECT_ROOT, SOURCE_SVG)),
    `${SOURCE_SVG} is missing — the committed PNG would have no source to be re-rendered from`,
  );
  assert.notEqual(iconPath(), SOURCE_SVG);
});

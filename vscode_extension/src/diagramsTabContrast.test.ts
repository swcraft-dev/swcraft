// The contrast check of the diagram tab's palette: LikeC4 1.60.0's palette with two changes
// and the marks in Tailwind CSS v3's amber and red, in VS Code's light and dark themes — the
// values of `likec4Palette.ts`, which TAB_STYLE is built from — measured against the canvas,
// against each other, and the marks against every surface a mark can border. It checks and
// never generates: a figure below its threshold is a named expected exception with its value,
// never tweaked.
//
// The sources, pinned (read as text, never run): `@likec4/core` 1.60.0
// (`dist/chunks/types.mjs`) and `@likec4/diagram` 1.60.0 (`styles.css`,
// `dist/base-primitives/compound/CompoundNodeContainer.mjs`), MIT; `tailwindcss` 3.4.17
// (`src/public/colors.js`), MIT; VS Code 1.145.2's default themes, read by
// scripts/extract-theme-colours.mjs into src/fixtures/theme-colours/vscode-1.145.2.json, for
// the one theme colour the palette still draws with, a match's fill
// (`editor.findMatchHighlightBackground`).
//
// Relative colours and `color-mix` are computed by a transcription of CSS Color 4/5 (§ 9's
// OKLab matrices, OKLCh mixing on the shorter hue arc); an out-of-gamut channel is clipped to
// sRGB, as an sRGB display paints it.
//
// Thresholds: WCAG 2.2's 4.5:1 for text (1.4.3) and 3:1 for a graphical boundary (1.4.11), a
// colour with alpha composited first. A match's fill must differ from the box's own fill by at
// least CIE76 ΔE 2.3 (one just noticeable difference). A fill against another fill carries no
// threshold (a box is bounded by its stroke), so those figures are pinned as measured.
//
// What this does not reach: LikeC4's runtime, a later VS Code or a third-party theme, a mark's
// outline against the match fill inside it, the stroke width, hue (WCAG measures lightness
// only), antialiasing, and the look as drawn.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";
import { LIKEC4_VERSION, type Likec4Role, likec4Palette, type Scheme } from "./likec4Palette";

// --- colour arithmetic --------------------------------------------------------------------

type Rgba = [number, number, number, number];

function linear(channel: number): number {
  const value = channel / 255;
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
}

function gamma(value: number): number {
  const clipped = Math.min(1, Math.max(0, value));
  return (clipped <= 0.0031308 ? 12.92 * clipped : 1.055 * clipped ** (1 / 2.4) - 0.055) * 255;
}

/** sRGB (0–255) to OKLCh: lightness 0–1, chroma, hue in degrees (CSS Color 4 § 9). */
function oklch(colour: Rgba): [number, number, number] {
  const [r, g, b] = colour.slice(0, 3).map(linear);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  const lightness = 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s;
  const a = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s;
  const bb = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s;
  return [lightness, Math.hypot(a, bb), ((Math.atan2(bb, a) * 180) / Math.PI + 360) % 360];
}

/** OKLCh back to sRGB (0–255), each channel clipped to the sRGB gamut. */
function fromOklch([lightness, chroma, hue]: [number, number, number]): Rgba {
  const a = chroma * Math.cos((hue * Math.PI) / 180);
  const b = chroma * Math.sin((hue * Math.PI) / 180);
  const l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (lightness - 0.0894841775 * a - 1.291485548 * b) ** 3;
  return [
    gamma(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    gamma(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    gamma(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s),
    1,
  ];
}

/** `color-mix(in oklch, a, b <weight of b>)`, the shorter hue arc (CSS Color 5 § 2). */
function mixOklch(a: Rgba, b: Rgba, weightB: number): [number, number, number] {
  const [p, q] = [oklch(a), oklch(b)];
  let delta = q[2] - p[2];
  if (delta > 180) delta -= 360;
  if (delta < -180) delta += 360;
  return [
    p[0] * (1 - weightB) + q[0] * weightB,
    p[1] * (1 - weightB) + q[1] * weightB,
    (p[2] + delta * weightB + 360) % 360,
  ];
}

/** The CSS colours the palette and the themes write: hex (3, 4, 6, 8) and LikeC4's two forms. */
export function parseCss(text: string): Rgba {
  const colour = text.trim();
  if (colour.startsWith("#")) {
    let hex = colour.slice(1);
    if (hex.length <= 4) hex = [...hex].map((digit) => digit + digit).join("");
    const byte = (at: number) => Number.parseInt(hex.slice(at, at + 2), 16);
    return [byte(0), byte(2), byte(4), hex.length === 8 ? byte(6) / 255 : 1];
  }
  const alpha = /^oklch\(from (#[0-9a-fA-F]{3,8}) l c h \/ ([\d.]+)%\)$/.exec(colour);
  if (alpha) {
    const base = parseCss(alpha[1]);
    return [base[0], base[1], base[2], Number(alpha[2]) / 100];
  }
  // `color-mix(in srgb, A N%, B)`: A at N % over B, mixed in gamma-encoded sRGB (CSS
  // Color 5 § 2) — the same arithmetic as a source-over composite.
  const mix = /^color-mix\(in srgb, (#[0-9a-fA-F]{3,8}) ([\d.]+)%, (#[0-9a-fA-F]{3,8})\)$/.exec(
    colour,
  );
  if (mix) {
    const top = parseCss(mix[1]);
    return over([top[0], top[1], top[2], Number(mix[2]) / 100], parseCss(mix[3]));
  }
  const title =
    /^oklch\(from color-mix\(in oklch, (#[0-9a-fA-F]{6}), (#[0-9a-fA-F]{6}) (\d+)%\) calc\(l ([+-]) \.(\d+)\) c h\)$/.exec(
      colour,
    );
  if (title) {
    const mixed = mixOklch(parseCss(title[1]), parseCss(title[2]), Number(title[3]) / 100);
    const shift = Number(`0.${title[5]}`) * (title[4] === "+" ? 1 : -1);
    return fromOklch([mixed[0] + shift, mixed[1], mixed[2]]);
  }
  throw new Error(`not a colour this check reads: ${text}`);
}

/** *top* composited over an opaque *under* (source-over). */
export function over(top: Rgba, under: Rgba): Rgba {
  const alpha = top[3];
  return [0, 1, 2].map((i) => top[i] * alpha + under[i] * (1 - alpha)).concat(1) as Rgba;
}

function luminance(colour: Rgba): number {
  return 0.2126 * linear(colour[0]) + 0.7152 * linear(colour[1]) + 0.0722 * linear(colour[2]);
}

/** WCAG 2.2's contrast ratio of two opaque colours. */
export function contrast(a: Rgba, b: Rgba): number {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}

function lab(colour: Rgba): [number, number, number] {
  const [r, g, b] = colour.slice(0, 3).map(linear);
  const x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047;
  const y = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  const z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883;
  const f = (t: number) => (t > 216 / 24389 ? Math.cbrt(t) : ((24389 / 27) * t + 16) / 116);
  return [116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))];
}

/** CIE76 ΔE of two opaque colours (sRGB, D65). */
export function deltaE(a: Rgba, b: Rgba): number {
  const [p, q] = [lab(a), lab(b)];
  return Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]);
}

const hex = (colour: Rgba) =>
  `#${colour
    .slice(0, 3)
    .map((channel) => Math.round(channel).toString(16).padStart(2, "0"))
    .join("")}`;

test("the arithmetic: WCAG's black on white, an alpha composite, ΔE, OKLCh both ways", () => {
  assert.equal(contrast(parseCss("#000"), parseCss("#fff")).toFixed(2), "21.00");
  assert.deepEqual(over([255, 255, 255, 0.25], parseCss("#000000")), [63.75, 63.75, 63.75, 1]);
  assert.equal(deltaE(parseCss("#ffffff"), parseCss("#000000")).toFixed(2), "100.00");
  assert.deepEqual(parseCss("#EA5C0055").slice(0, 3), [234, 92, 0]);
  // OKLCh round trip of an in-gamut colour, and CSS Color 4's own example value:
  // `#3b82f6` is about oklch(62.3% 0.188 259.8).
  assert.equal(hex(fromOklch(oklch(parseCss("#3b82f6")))), "#3b82f6");
  const [l, c, h] = oklch(parseCss("#3b82f6"));
  assert.deepEqual([l.toFixed(3), c.toFixed(3), h.toFixed(1)], ["0.623", "0.188", "259.8"]);
  assert.deepEqual(parseCss("oklch(from #3b82f6 l c h / 15%)"), [59, 130, 246, 0.15]);
});

// --- the palette ---------------------------------------------------------------------------

/** A scheme's role, as a colour. */
function colourOf(scheme: Scheme, role: Likec4Role): Rgba {
  return parseCss(likec4Palette(scheme)[role]);
}

test("the compound titles, computed: LikeC4's color-mix in OKLCh with the lightness shifted", () => {
  assert.equal(LIKEC4_VERSION, "1.60.0");
  assert.equal(hex(colourOf("light", "title")), "#3566b4");
  // Out of the sRGB gamut in its blue channel, clipped as an sRGB display paints it.
  assert.equal(hex(colourOf("dark", "title")), "#ade5ff");
});

/** The surfaces a box, a label or a mark lies on, per scheme. */
function surfaces(scheme: Scheme): Record<string, Rgba> {
  const canvas = colourOf(scheme, "canvas");
  const compound = over(colourOf(scheme, "compoundFill"), canvas);
  return {
    canvas,
    compound: compound,
    "nested compound": over(colourOf(scheme, "compoundFill"), compound),
    element: colourOf(scheme, "fill"),
    "port fill": colourOf(scheme, "portFill"),
  };
}

interface Figure {
  ratio: number;
  deltaE: number;
  threshold: number;
}

function measure(ink: Rgba, ground: Rgba, threshold: number): Figure {
  const seen = over(ink, ground);
  return { ratio: contrast(seen, ground), deltaE: deltaE(seen, ground), threshold };
}

/** Every palette figure of a scheme, keyed `<check> on|against <surface>`. */
function paletteFigures(scheme: Scheme): Map<string, Figure> {
  const on = surfaces(scheme);
  const colour = (role: Likec4Role) => colourOf(scheme, role);
  const figures = new Map<string, Figure>();
  const add = (check: string, role: Likec4Role, where: string[], threshold: number) => {
    for (const ground of where)
      figures.set(`${check} on ${ground}`, measure(colour(role), on[ground], threshold));
  };
  // Text: an element's label, a lifeline's detail, a compound's heading, the port labels.
  add("element label (hiContrast)", "text", ["element"], 4.5);
  add("lifeline detail (loContrast)", "detail", ["element"], 4.5);
  add("compound title", "title", ["compound", "nested compound"], 4.5);
  add("port label", "portLabel", ["canvas", "compound"], 4.5);
  add("member port label", "memberPortLabel", ["element"], 4.5);
  // Inside a member composition: the port label grey on that composition's tint, which lies
  // on the frame's.
  add("port label in a member composition", "portLabel", ["nested compound"], 4.5);
  // Boundaries: the element's stroke, the compound's border, the lines and port squares.
  add("element stroke", "stroke", ["canvas", "compound", "nested compound"], 3);
  add("compound border", "compoundBorder", ["canvas", "compound"], 3);
  add("line", "line", ["canvas", "compound", "nested compound"], 3);
  // A port: its outline (the line grey) and its lit outline (the selection) against its fill,
  // the group's colour.
  add("port outline (line)", "line", ["port fill"], 3);
  add("lit port outline (selection)", "selection", ["port fill"], 3);
  // The marks: the selection amber, the current match red, a match red-300.
  for (const mark of ["selection", "current", "hit"] as const) {
    add(`${mark} mark`, mark, ["canvas", "compound", "nested compound", "element"], 3);
  }
  // The fills against each other: no threshold, separated by the strokes.
  for (const [a, b] of [
    ["compound", "canvas"],
    ["nested compound", "compound"],
    ["element", "canvas"],
    ["element", "compound"],
  ]) {
    figures.set(`${a} fill against ${b}`, {
      ratio: contrast(on[a], on[b]),
      deltaE: deltaE(on[a], on[b]),
      threshold: Number.NaN,
    });
  }
  return figures;
}

function passes(figure: Figure): boolean {
  return Number.isNaN(figure.threshold) ? figure.deltaE >= 2.3 : figure.ratio >= figure.threshold;
}

/** Every palette figure, both schemes, as `ratio:1` (and ΔE for a fill pair). */
function printed(): Record<string, string> {
  const out: Record<string, string> = {};
  for (const scheme of ["light", "dark"] as const) {
    for (const [check, figure] of paletteFigures(scheme)) {
      out[`${scheme} | ${check}`] = Number.isNaN(figure.threshold)
        ? `${figure.ratio.toFixed(2)}:1, ΔE ${figure.deltaE.toFixed(2)}`
        : `${figure.ratio.toFixed(2)}:1${passes(figure) ? "" : ` (below ${figure.threshold})`}`;
    }
  }
  return out;
}

/**
 * Every figure of the palette and the marks, pinned as measured. A figure marked *below* is a
 * named expected exception: the colours are LikeC4's and Tailwind's as published, with two
 * changes; none is tweaked here. The weak spots: the light amber selection on the near-white
 * canvas and compound; every mark on the blue element, whose luminance is close to the marks'
 * (hue separates them, which WCAG does not measure); the line grey and the port label on the
 * light compound; the element's stroke on the dark canvas. A port's fill is the 15 % compound
 * over the canvas, so its outline and lit outline measure as the line and the selection on the
 * compound (2.79 and 1.81 light, below 3; the lit outline's 2 px and amber hue carry it). A
 * port label inside a member composition measures 2.40 light (below 4.5), 6.49 dark.
 */
const FIGURES: Record<string, string> = {
  "light | element label (hiContrast) on element": "3.38:1 (below 4.5)",
  "light | lifeline detail (loContrast) on element": "2.59:1 (below 4.5)",
  "light | compound title on compound": "4.78:1",
  "light | compound title on nested compound": "4.10:1 (below 4.5)",
  "light | port label on canvas": "3.32:1 (below 4.5)",
  "light | port label on compound": "2.79:1 (below 4.5)",
  "light | member port label on element": "3.38:1 (below 4.5)",
  "light | port label in a member composition on nested compound": "2.40:1 (below 4.5)",
  "light | element stroke on canvas": "5.17:1",
  "light | element stroke on compound": "4.35:1",
  "light | element stroke on nested compound": "3.73:1",
  "light | compound border on canvas": "2.43:1 (below 3)",
  "light | compound border on compound": "2.24:1 (below 3)",
  "light | line on canvas": "3.32:1",
  "light | line on compound": "2.79:1 (below 3)",
  "light | line on nested compound": "2.40:1 (below 3)",
  "light | port outline (line) on port fill": "2.79:1 (below 3)",
  "light | lit port outline (selection) on port fill": "1.81:1 (below 3)",
  "light | selection mark on canvas": "2.15:1 (below 3)",
  "light | selection mark on compound": "1.81:1 (below 3)",
  "light | selection mark on nested compound": "1.55:1 (below 3)",
  "light | selection mark on element": "1.71:1 (below 3)",
  "light | current mark on canvas": "4.83:1",
  "light | current mark on compound": "4.06:1",
  "light | current mark on nested compound": "3.49:1",
  "light | current mark on element": "1.31:1 (below 3)",
  "light | hit mark on canvas": "1.90:1 (below 3)",
  "light | hit mark on compound": "1.60:1 (below 3)",
  "light | hit mark on nested compound": "1.37:1 (below 3)",
  "light | hit mark on element": "1.94:1 (below 3)",
  "light | compound fill against canvas": "1.19:1, ΔE 11.77",
  "light | nested compound fill against compound": "1.17:1, ΔE 10.10",
  "light | element fill against canvas": "3.68:1, ΔE 80.16",
  "light | element fill against compound": "3.10:1, ΔE 68.78",
  "dark | element label (hiContrast) on element": "3.38:1 (below 4.5)",
  "dark | lifeline detail (loContrast) on element": "2.59:1 (below 4.5)",
  "dark | compound title on compound": "9.45:1",
  "dark | compound title on nested compound": "7.88:1",
  "dark | port label on canvas": "9.37:1",
  "dark | port label on compound": "7.79:1",
  "dark | member port label on element": "2.22:1 (below 4.5)",
  "dark | port label in a member composition on nested compound": "6.49:1",
  "dark | element stroke on canvas": "3.00:1",
  "dark | element stroke on compound": "2.50:1 (below 3)",
  "dark | element stroke on nested compound": "2.08:1 (below 3)",
  "dark | compound border on canvas": "1.81:1 (below 3)",
  "dark | compound border on compound": "1.67:1 (below 3)",
  "dark | line on canvas": "4.68:1",
  "dark | line on compound": "3.89:1",
  "dark | line on nested compound": "3.24:1",
  "dark | port outline (line) on port fill": "3.89:1",
  "dark | lit port outline (selection) on port fill": "7.73:1",
  "dark | selection mark on canvas": "9.30:1",
  "dark | selection mark on compound": "7.73:1",
  "dark | selection mark on nested compound": "6.44:1",
  "dark | selection mark on element": "2.20:1 (below 3)",
  "dark | current mark on canvas": "5.61:1",
  "dark | current mark on compound": "4.66:1",
  "dark | current mark on nested compound": "3.89:1",
  "dark | current mark on element": "1.33:1 (below 3)",
  "dark | hit mark on canvas": "8.18:1",
  "dark | hit mark on compound": "6.80:1",
  "dark | hit mark on nested compound": "5.66:1",
  "dark | hit mark on element": "1.94:1 (below 3)",
  "dark | compound fill against canvas": "1.20:1, ΔE 13.89",
  "dark | nested compound fill against compound": "1.20:1, ΔE 10.89",
  "dark | element fill against canvas": "4.22:1, ΔE 78.59",
  "dark | element fill against compound": "3.51:1, ΔE 65.02",
};

test("the palette's figures, every one pinned; those below threshold are its named exceptions", () => {
  const measured = printed();
  assert.ok(Object.keys(measured).length > 50);
  assert.deepEqual(measured, FIGURES);
});

// --- the theme colour the palette themes still draw with -----------------------------------

interface ThemeColours {
  file: string;
  kind: "light" | "dark" | "hcDark" | "hcLight";
  colours: Record<string, string | null>;
  unresolved: Record<string, string>;
}

const FIXTURE = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, "..", "src", "fixtures", "theme-colours", "vscode-1.145.2.json"),
    "utf8",
  ),
) as { vscodeVersion: string; themes: Record<string, ThemeColours> };

test("the fixture holds VS Code 1.145.2's ten default themes, every match fill resolved", () => {
  assert.equal(FIXTURE.vscodeVersion, "1.145.2");
  const kinds = Object.values(FIXTURE.themes).map((theme) => theme.kind);
  assert.equal(kinds.filter((kind) => kind === "light").length, 4);
  assert.equal(kinds.filter((kind) => kind === "dark").length, 4);
  assert.equal(kinds.length, 10);
  for (const [name, theme] of Object.entries(FIXTURE.themes)) {
    if (theme.kind !== "light" && theme.kind !== "dark") continue;
    assert.equal(typeof theme.colours["editor.findMatchHighlightBackground"], "string", name);
  }
});

/** A match's fill (the theme's find highlight over what lies beneath) against the box's own fill. */
function hitFills(): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [themeName, theme] of Object.entries(FIXTURE.themes)) {
    if (theme.kind !== "light" && theme.kind !== "dark") continue;
    const on = surfaces(theme.kind);
    const hit = parseCss(theme.colours["editor.findMatchHighlightBackground"] ?? "");
    for (const box of ["element", "compound"]) {
      const beneath = box === "element" ? "compound" : "canvas";
      const seen = over(hit, on[beneath]);
      const figure = { ratio: contrast(seen, on[box]), deltaE: deltaE(seen, on[box]) };
      out[`${theme.kind} | ${themeName} | match fill over ${beneath} against the ${box}`] =
        `${figure.ratio.toFixed(2)}:1, ΔE ${figure.deltaE.toFixed(2)}${figure.deltaE >= 2.3 ? "" : " (below ΔE 2.3)"}`;
    }
  }
  return out;
}

/**
 * A match's fill against the box it marks, per theme (the find highlight is the theme's).
 * The smallest difference: Light 2026's blue find highlight (#0069CC1A) over the canvas,
 * seen against the light compound (ΔE 3.93, still above one noticeable difference).
 */
const HIT_FILLS: Record<string, string> = {
  "light | Light 2026 | match fill over compound against the element": "2.70:1, ΔE 62.27",
  "light | Light 2026 | match fill over canvas against the compound": "1.03:1, ΔE 3.93",
  "dark | Dark 2026 | match fill over compound against the element": "2.46:1, ΔE 58.45",
  "dark | Dark 2026 | match fill over canvas against the compound": "1.27:1, ΔE 10.88",
  "dark | Dark+ | match fill over compound against the element": "2.41:1, ΔE 86.81",
  "dark | Dark+ | match fill over canvas against the compound": "1.30:1, ΔE 44.44",
  "dark | Dark Modern | match fill over compound against the element": "2.41:1, ΔE 86.81",
  "dark | Dark Modern | match fill over canvas against the compound": "1.30:1, ΔE 44.44",
  "light | Light+ | match fill over compound against the element": "2.12:1, ΔE 83.15",
  "light | Light+ | match fill over canvas against the compound": "1.27:1, ΔE 34.95",
  "light | Light Modern | match fill over compound against the element": "2.12:1, ΔE 83.15",
  "light | Light Modern | match fill over canvas against the compound": "1.27:1, ΔE 34.95",
  "dark | Visual Studio Dark | match fill over compound against the element": "2.41:1, ΔE 86.81",
  "dark | Visual Studio Dark | match fill over canvas against the compound": "1.30:1, ΔE 44.44",
  "light | Visual Studio Light | match fill over compound against the element": "2.12:1, ΔE 83.15",
  "light | Visual Studio Light | match fill over canvas against the compound": "1.27:1, ΔE 34.95",
};

test("a match's fill differs from every LikeC4 fill it lands on; the figures, pinned", () => {
  const measured = hitFills();
  assert.equal(Object.values(measured).filter((figure) => figure.includes("below")).length, 0);
  assert.deepEqual(measured, HIT_FILLS);
});

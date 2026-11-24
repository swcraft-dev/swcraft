// LikeC4's published diagram palette, the one the diagram tab takes in VS Code's light and dark
// themes, with two local changes. A leaf: no import, no `vscode`; `diagramsTab.ts` builds
// `TAB_STYLE`'s palette declarations from it and `diagramsTabContrast.test.ts` measures it.
//
// The source, pinned: LikeC4 1.60.0, the npm packages `@likec4/core` and `@likec4/diagram` 1.60.0
// (MIT, each package's `license` field and LICENSE file), read as text from the published
// tarballs, never installed or run:
// - `@likec4/core` `dist/chunks/types.mjs`: the element colour `primary`
//   (`{fill:'#3b82f6', stroke:'#2563eb', hiContrast:'#eff6ff', loContrast:'#bfdbfe'}`),
//   the relationship colour `gray` (`{line:'#8D8D8D', labelBg:'#18191B',
//   label:'#C9C9C9'}`) and the defaults (`{color:'primary', opacity:15,
//   group:{opacity:15, border:'dashed'}, relationship:{color:'gray', …}}`).
// - `@likec4/diagram` `dist/base-primitives/compound/CompoundNodeContainer.mjs`: a compound of
//   opacity 15 is transparent, its border at `50 + 50 × 15 / 100` = 57.5 %
//   (`--_border-transparency`), its fill at 15 % (`--_compound-transparency`), and
//   `inverseColor` (opacity < 70).
// - `@likec4/diagram` `styles.css`: `.likec4-compound-node` `--_border-width:3px`;
//   `.likec4-compound-node--isTransparent_true` `border-color: oklch(from stroke l c h /
//   border-transparency)`, `background-color: oklch(from fill l c h /
//   compound-transparency)`; `.likec4-compound-node--inverseColor_true` the title colour
//   `oklch(from color-mix(in oklch, hiContrast, stroke 60%) calc(l - .2) c h)`, and under
//   `[data-mantine-color-scheme=dark]` `calc(l + .2)`; the canvas
//   `--colors-surface-canvas` `#fff`, and under `[data-mantine-color-scheme=dark]` `#242424`.
//
// The two changes to "exactly as published": (1) a compound's border is **solid**, not LikeC4's
// `dashed` group border (this module carries no border style); (2) in the **light** theme a port's
// label inside a component box takes the component's text colour (`MEMBER_PORT_LABEL.light`).
//
// The marks (selection, hover, pin highlight with its arrowheads, current and other search
// matches) take Tailwind CSS v3's default colours, the family LikeC4's `primary` comes from
// (blue-500, blue-600, blue-50, blue-200). Pinned: `tailwindcss` 3.4.17 (MIT),
// `src/public/colors.js`, read as text: `amber[400]` `#fbbf24`, `amber[500]` `#f59e0b`,
// `red[300]` `#fca5a5`, `red[400]` `#f87171`, `red[600]` `#dc2626`. Every mark 4 px (LikeC4's
// outline size), solid; a match's fill stays VS Code's `editor.findMatchHighlightBackground`.
//
// Ports and compositions: (1) a lit (`highlight`: pin or hover) or `selected` port draws its
// square's and glyph's outlines in the selection colour at `PORT_LIT_WIDTH`, between the
// unselected outline (1 for the square, 1.2 for the glyph) and a lit connector (`MARK_WIDTH`), all
// scaling with the zoom so the order holds; (2) the square's outline, the glyph's outline and the
// connector take the `gray` relationship line; (3) a member of kind `composition` takes the
// compound look on every view; (4) a port's square and glyph inside take `portFill`, the compound
// fill over the canvas as one opaque colour.
//
// What this does not reach: LikeC4's runtime (its React renderer, a user's `likec4`
// specification overriding a colour or the defaults), a later LikeC4 release, and ports, which
// LikeC4 has no notion of: their colours (`portFill`, the line colour, a label in the line colour
// in a light theme and the label colour in a dark one) are SWCraft's mapping. LikeC4's
// relationships are dashed by default; the tab keeps the generator's solid lines.

/** The LikeC4 release the values are read from. */
export const LIKEC4_VERSION = "1.60.0";

/** The element colour `primary`, LikeC4's default element colour (`@likec4/core` `dist/chunks/types.mjs`). */
export const LIKEC4_PRIMARY = {
  /** `primary.fill` */
  fill: "#3b82f6",
  /** `primary.stroke` */
  stroke: "#2563eb",
  /** `primary.hiContrast` */
  hiContrast: "#eff6ff",
  /** `primary.loContrast` */
  loContrast: "#bfdbfe",
} as const;

/** The relationship colour `gray`, LikeC4's default relationship colour (`@likec4/core` `dist/chunks/types.mjs`). */
export const LIKEC4_GRAY_RELATIONSHIP = {
  /** `gray.line` */
  line: "#8D8D8D",
  /** `gray.label` */
  label: "#C9C9C9",
} as const;

/** LikeC4's default element opacity, which a compound takes: 15 (per cent). */
export const LIKEC4_COMPOUND_OPACITY = 15;

/** A compound's border opacity, `50 + 50 × opacity / 100` (`CompoundNodeContainer.mjs`). */
export const LIKEC4_COMPOUND_BORDER_OPACITY = 50 + (50 * LIKEC4_COMPOUND_OPACITY) / 100;

/** A compound's border width (`styles.css`, `.likec4-compound-node` `--_border-width`). */
export const LIKEC4_COMPOUND_BORDER_WIDTH = "3px";

/** The two colour schemes the tab maps: VS Code's light and dark themes. */
export type Scheme = "light" | "dark";

/** The diagram canvas (`@likec4/diagram` `styles.css`, `--colors-surface-canvas`). */
export const LIKEC4_CANVAS: Readonly<Record<Scheme, string>> = {
  /** `--colors-surface-canvas:#fff` */
  light: "#fff",
  /** `[data-mantine-color-scheme=dark]` `--colors-surface-canvas:#242424` */
  dark: "#242424",
};

/** A compound's fill: the element fill at the compound opacity (`isTransparent_true`). */
export function compoundFill(fill: string): string {
  return `oklch(from ${fill} l c h / ${LIKEC4_COMPOUND_OPACITY}%)`;
}

/** A compound's border: the element stroke at the border opacity (`isTransparent_true`). */
export function compoundBorder(stroke: string): string {
  return `oklch(from ${stroke} l c h / ${LIKEC4_COMPOUND_BORDER_OPACITY}%)`;
}

/** A compound's title (`inverseColor_true`): the light scheme's `l - .2`, the dark's `l + .2`. */
export function compoundTitle(hiContrast: string, stroke: string, scheme: Scheme): string {
  const shift = scheme === "light" ? "- .2" : "+ .2";
  return `oklch(from color-mix(in oklch, ${hiContrast}, ${stroke} 60%) calc(l ${shift}) c h)`;
}

/**
 * A port's label (SWCraft's mapping: LikeC4 has no port): outside a box the relationship
 * line colour in a light theme, the relationship label colour in a dark one.
 */
export const PORT_LABEL: Readonly<Record<Scheme, string>> = {
  light: LIKEC4_GRAY_RELATIONSHIP.line,
  dark: LIKEC4_GRAY_RELATIONSHIP.label,
};

/**
 * A port's label inside a component box: in the light theme the component's own text colour, in
 * the dark one as outside.
 */
export const MEMBER_PORT_LABEL: Readonly<Record<Scheme, string>> = {
  light: LIKEC4_PRIMARY.hiContrast,
  dark: LIKEC4_GRAY_RELATIONSHIP.label,
};

/** The Tailwind CSS v3 default colours the marks take (`tailwindcss` 3.4.17, `src/public/colors.js`). */
export const TAILWIND_V3 = {
  /** `amber[400]` */
  amber400: "#fbbf24",
  /** `amber[500]` */
  amber500: "#f59e0b",
  /** `red[300]` */
  red300: "#fca5a5",
  /** `red[400]` */
  red400: "#f87171",
  /** `red[600]` */
  red600: "#dc2626",
} as const;

/** The width of every mark outline in the palette themes (LikeC4's outline size). */
export const MARK_WIDTH = "4px";

/**
 * A lit or selected port's square and glyph outline: between the unselected port's outline (1 and
 * 1.2, the renderer's) and the lit connector's `MARK_WIDTH`; it scales with the zoom as they do.
 */
export const PORT_LIT_WIDTH = "2px";

/**
 * A port's fill: the enclosing group's colour, the compound fill (*fill* at the compound opacity)
 * over *canvas*, as one opaque colour.
 */
export function portFill(fill: string, canvas: string): string {
  return `color-mix(in srgb, ${fill} ${LIKEC4_COMPOUND_OPACITY}%, ${canvas})`;
}

/** The marks per scheme. */
export const MARKS: Readonly<
  Record<Scheme, Readonly<Record<"selection" | "current" | "hit", string>>>
> = {
  light: {
    selection: TAILWIND_V3.amber500,
    current: TAILWIND_V3.red600,
    hit: TAILWIND_V3.red300,
  },
  dark: {
    selection: TAILWIND_V3.amber400,
    current: TAILWIND_V3.red400,
    hit: TAILWIND_V3.red300,
  },
};

/** The roles TAB_STYLE carries, one custom property each per scheme. */
export type Likec4Role =
  | "canvas"
  | "fill"
  | "stroke"
  | "text"
  | "detail"
  | "compoundFill"
  | "compoundBorder"
  | "title"
  | "line"
  | "portFill"
  | "portLabel"
  | "memberPortLabel"
  | "selection"
  | "current"
  | "hit";

/** A scheme's palette: each role's CSS value. */
export function likec4Palette(scheme: Scheme): Readonly<Record<Likec4Role, string>> {
  const primary = LIKEC4_PRIMARY;
  return {
    canvas: LIKEC4_CANVAS[scheme],
    fill: primary.fill,
    stroke: primary.stroke,
    text: primary.hiContrast,
    detail: primary.loContrast,
    compoundFill: compoundFill(primary.fill),
    compoundBorder: compoundBorder(primary.stroke),
    title: compoundTitle(primary.hiContrast, primary.stroke, scheme),
    line: LIKEC4_GRAY_RELATIONSHIP.line,
    portFill: portFill(primary.fill, LIKEC4_CANVAS[scheme]),
    portLabel: PORT_LABEL[scheme],
    memberPortLabel: MEMBER_PORT_LABEL[scheme],
    selection: MARKS[scheme].selection,
    current: MARKS[scheme].current,
    hit: MARKS[scheme].hit,
  };
}

/** The CSS custom property TAB_STYLE carries a role's value in: `--swcraft-likec4-<role>`. */
export function likec4Variable(role: Likec4Role): string {
  return `--swcraft-likec4-${role}`;
}

/** A scheme's palette as one rule body of custom properties, in the roles' order. */
export function likec4Declarations(scheme: Scheme): string {
  const palette = likec4Palette(scheme);
  return (Object.keys(palette) as Likec4Role[])
    .map((role) => `${likec4Variable(role)}: ${palette[role]};`)
    .join(" ");
}

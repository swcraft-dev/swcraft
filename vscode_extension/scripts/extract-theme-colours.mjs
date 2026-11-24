// Reads the colours the diagram tab uses out of an installed VS Code's default
// themes into src/fixtures/theme-colours/vscode-<version>.json, the input of
// src/diagramsTabContrast.test.ts.

//
// usage: node scripts/extract-theme-colours.mjs [VSCODE_APP_DIR]
//   VSCODE_APP_DIR defaults to the macOS install,
//   "/Applications/Visual Studio Code.app/Contents/Resources/app".
//
// It is a reader, never a generator of colours: every value comes from a file of
// the installed application, read as text, and nothing of the application is
// executed.
//   1. The theme list and each theme's kind come from
//      extensions/theme-defaults/package.json (`contributes.themes`, `uiTheme`:
//      vs, vs-dark, hc-black, hc-light).
//   2. A theme file is JSON with comments: comments outside strings and trailing
//      commas are removed, and its `include` chain is followed (the including
//      file's colours win).
//   3. An id the theme chain leaves unset is looked up in the colour registry's
//      defaults: the `registerColor(id, {light, dark, hcDark, hcLight}, ...)`
//      calls of out/vs/workbench/workbench.desktop.main.js, read as text. The
//      minified name of registerColor is learnt from the `focusBorder` call. A
//      default that is a hex literal is taken, recorded with the call's byte
//      offset; a default that names another registered colour (a minified
//      variable bound by its own registerColor call) is resolved as that colour
//      in the same theme, recorded with the chain; `null` means the colour is
//      unset in that kind; any other default (a computed one, such as
//      `transparent(foreground, .7)`) is listed as unresolved, never guessed.
//
// What it does not reach: a theme outside theme-defaults, a user's
// `workbench.colorCustomizations`, and a computed registry default.

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

/** The colour ids the diagram tab names or that lie beneath its marks. */
export const COLOUR_IDS = [
  "editor.background",
  "editor.foreground",
  "descriptionForeground",
  "focusBorder",
  "contrastBorder",
  "editor.selectionBackground",
  "editor.findMatchBackground",
  "editor.findMatchHighlightBackground",
  "editor.findMatchBorder",
  "editor.findMatchHighlightBorder",
  "charts.yellow",
  "charts.red",
];

const UI_THEME_KIND = {
  vs: "light",
  "vs-dark": "dark",
  "hc-black": "hcDark",
  "hc-light": "hcLight",
};

/** *text* without its comments outside strings and without trailing commas. */
export function stripJsonc(text) {
  let out = "";
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (c === '"') {
      let j = i + 1;
      while (j < text.length && text[j] !== '"') j += text[j] === "\\" ? 2 : 1;
      out += text.slice(i, j + 1);
      i = j + 1;
    } else if (text.startsWith("//", i)) {
      const end = text.indexOf("\n", i);
      i = end === -1 ? text.length : end;
    } else if (text.startsWith("/*", i)) {
      const end = text.indexOf("*/", i + 2);
      i = end === -1 ? text.length : end + 2;
    } else {
      out += c;
      i += 1;
    }
  }
  return out.replace(/,(\s*[}\]])/g, "$1");
}

function themeColours(themesDir, file, seen = new Set()) {
  if (seen.has(file)) throw new Error(`an include cycle at ${file}`);
  seen.add(file);
  const data = JSON.parse(stripJsonc(readFileSync(path.join(themesDir, file), "utf8")));
  const colours = {};
  const sources = {};
  if (typeof data.include === "string") {
    const included = themeColours(themesDir, path.posix.basename(data.include), seen);
    Object.assign(colours, included.colours);
    Object.assign(sources, included.sources);
  }
  for (const [id, value] of Object.entries(data.colors ?? {})) {
    colours[id] = value;
    sources[id] = `theme ${file}`;
  }
  return { colours, sources };
}

/** The registry's defaults: id -> {spec, offset}, and minified variable -> id. */
export function readRegistry(js) {
  const named = /([\w$]+)\("focusBorder",\{/.exec(js);
  if (!named) throw new Error("the focusBorder registerColor call is not found");
  const fn = named[1].replace(/\$/g, "\\$");
  const call = new RegExp(`(?:([\\w$]+)=)?${fn}\\("([\\w.]+)",(\\{[^{}]*\\}|[\\w$]+)`, "g");
  const byId = new Map();
  const byVariable = new Map();
  for (let m = call.exec(js); m !== null; m = call.exec(js)) {
    const [, variable, id, spec] = m;
    if (!byId.has(id)) byId.set(id, { spec, offset: m.index });
    if (variable) byVariable.set(variable, id);
  }
  return { byId, byVariable };
}

function kindValue(spec, kind) {
  if (!spec.startsWith("{")) return spec; // a bare reference: the same for every kind
  const m = new RegExp(`[{,]${kind}:(.*?)(?=,(?:light|dark|hcDark|hcLight):|\\}$)`).exec(spec);
  return m ? m[1] : undefined;
}

function resolve(id, kind, theme, registry, chain = []) {
  if (theme.colours[id] !== undefined) {
    return { value: theme.colours[id], source: [...chain, theme.sources[id]].join(" <- ") };
  }
  const entry = registry.byId.get(id);
  if (!entry) return { unresolved: `no registerColor call for ${id}` };
  const raw = kindValue(entry.spec, kind);
  const where = `registry default of ${id} (${kind}) at byte ${entry.offset}`;
  if (raw === undefined) return { unresolved: `${where}: no ${kind} entry in ${entry.spec}` };
  if (raw === "null") return { value: null, source: [...chain, where].join(" <- ") };
  const literal = /^"(#[0-9a-fA-F]{3,8})"$/.exec(raw);
  if (literal) return { value: literal[1], source: [...chain, where].join(" <- ") };
  const other = registry.byVariable.get(raw);
  if (other && !chain.includes(other)) {
    return resolve(other, kind, theme, registry, [...chain, `${where} names ${other}`]);
  }
  return { unresolved: `${where}: a computed default (${raw}), not evaluated` };
}

function main() {
  const appDir = process.argv[2] ?? "/Applications/Visual Studio Code.app/Contents/Resources/app";
  const version = JSON.parse(readFileSync(path.join(appDir, "package.json"), "utf8")).version;
  const extension = path.join(appDir, "extensions", "theme-defaults");
  const themesDir = path.join(extension, "themes");
  const contributed = JSON.parse(readFileSync(path.join(extension, "package.json"), "utf8"))
    .contributes.themes;
  const registryFile = "out/vs/workbench/workbench.desktop.main.js";
  const registry = readRegistry(readFileSync(path.join(appDir, registryFile), "utf8"));
  const themes = {};
  for (const contribution of contributed) {
    const file = path.posix.basename(contribution.path);
    const kind = UI_THEME_KIND[contribution.uiTheme];
    const theme = themeColours(themesDir, file);
    const colours = {};
    const sources = {};
    const unresolved = {};
    for (const id of COLOUR_IDS) {
      const found = resolve(id, kind, theme, registry);
      if ("unresolved" in found) {
        unresolved[id] = found.unresolved;
      } else {
        colours[id] = found.value;
        sources[id] = found.source;
      }
    }
    themes[contribution.id] = { file, kind, colours, sources, unresolved };
  }
  const here = path.dirname(fileURLToPath(import.meta.url));
  const outDir = path.join(here, "..", "src", "fixtures", "theme-colours");
  mkdirSync(outDir, { recursive: true });
  const out = path.join(outDir, `vscode-${version}.json`);
  const document = {
    generator: "vscode_extension/scripts/extract-theme-colours.mjs",
    vscodeVersion: version,
    registry: registryFile,
    ids: COLOUR_IDS,
    themes,
  };
  writeFileSync(out, `${JSON.stringify(document, null, 2)}\n`, { encoding: "utf8" });
  process.stdout.write(`${out}\n`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}

/**
 * The `guide` lens's target: the guide page that describes a model file's
 * layer, at the section of the element's kind.
 *
 * Pure — no `vscode` import. The page and the anchors come from
 * `references.json` (`guidePage`, `guideAnchor`), which the generator writes
 * from the functions that name the guide's pages and headings, so this module
 * holds no rule about either.
 */

import { type References, topLevelKindOf, topLevelScalar } from "./navigationCore";

/** A guide section: the corpus page id and, when the kind is known, its heading's anchor. */
export interface GuideTarget {
  page: string;
  anchor?: string;
}

/**
 * The guide target of *text*, a file of *layer*, or `undefined` when the file
 * gets no lens row: the layer is unknown or the file has no single identity
 * line (the rule the lens row itself follows). A file whose kind cannot be read
 * opens the layer's page at its top.
 */
export function guideLens(refs: References, text: string, layer: string): GuideTarget | undefined {
  const facts = refs.layers.get(layer);
  if (facts === undefined || topLevelScalar(text, facts.identityField).status !== "one") {
    return undefined;
  }
  const kind = topLevelKindOf(text, facts);
  const anchor = facts.topLevelKinds.find((entry) => entry.kind === kind)?.guideAnchor;
  return { page: facts.guidePage, anchor };
}

/** The `guide` lens's command: `swcraft.openGuide` with *target* as its one argument. */
export function guideLensCommand(target: GuideTarget): {
  title: string;
  command: string;
  arguments: [GuideTarget];
} {
  return { title: "guide", command: "swcraft.openGuide", arguments: [target] };
}

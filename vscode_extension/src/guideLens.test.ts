// The `guide` lens's target, read off the shipped references.json and the
// example model's own files. No `vscode` import: this runs under plain `node --test`.

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import { guideLens, guideLensCommand } from "./guideLens";
import { guideCommandArg, sectionPagePath } from "./helpLinks";
import { loadReferences, type References } from "./navigationCore";
import { BASIC_MODEL, SCHEMAS_DIR } from "./testSupport";

function shipped(): References {
  return loadReferences(fs.readFileSync(path.join(SCHEMAS_DIR, "references.json"), "utf8"));
}

function basicFile(...parts: string[]): string {
  return fs.readFileSync(path.join(BASIC_MODEL, ...parts), "utf8");
}

test("an interface file's guide lens opens the interfaces page at its kind's section", () => {
  const refs = shipped();
  assert.deepEqual(guideLens(refs, basicFile("interfaces", "IExample.yaml"), "interfaces"), {
    page: "reference/model/interfaces",
    anchor: "sender_receiver",
  });
  assert.deepEqual(
    guideLens(refs, basicFile("interfaces", "IDiagnosticService.yaml"), "interfaces"),
    {
      page: "reference/model/interfaces",
      anchor: "client_server",
    },
  );
});

test("a one-kind layer's file opens its page at its one kind's section, an external component included", () => {
  const refs = shipped();
  const component = basicFile("swc", "example", "ExampleSWC.yaml");
  assert.deepEqual(guideLens(refs, component, "swc"), {
    page: "reference/model/swc",
    anchor: "swc",
  });
  assert.deepEqual(guideLens(refs, `${component}external: true\n`, "swc"), {
    page: "reference/model/swc",
    anchor: "swc",
  });
  assert.deepEqual(guideLens(refs, basicFile("tasks", "Task_10ms.yaml"), "tasks"), {
    page: "reference/model/tasks",
    anchor: "task",
  });
  assert.deepEqual(guideLens(refs, "id: REQ_X\ntext: x\n", "requirements"), {
    page: "reference/model/requirements",
    anchor: "requirement",
  });
});

test("every discriminator value of every multi-kind layer opens its own kind's section", () => {
  const refs = shipped();
  let checked = 0;
  for (const [layer, facts] of refs.layers) {
    if (facts.discriminator === null) {
      continue;
    }
    for (const entry of facts.topLevelKinds) {
      const text: string = `${facts.identityField}: Element\n${facts.discriminator}: ${entry.discriminatorValue}\n`;
      assert.deepEqual(
        guideLens(refs, text, layer),
        { page: facts.guidePage, anchor: entry.guideAnchor },
        `${layer} ${entry.kind}`,
      );
      checked += 1;
    }
  }
  assert.ok(checked > 3);
});

test("a stub kind opens its own section: external_interface and external_type", () => {
  const refs = shipped();
  assert.deepEqual(guideLens(refs, "name: IStub\nkind: external_interface\n", "interfaces"), {
    page: "reference/model/interfaces",
    anchor: "external_interface",
  });
  assert.deepEqual(guideLens(refs, "name: TStub\nkind: external_type\n", "implementation_types"), {
    page: "reference/model/implementation_types",
    anchor: "external_type",
  });
});

test("a file whose kind cannot be read opens the layer's page at its top", () => {
  const refs = shipped();
  const page = { page: "reference/model/interfaces", anchor: undefined };
  assert.deepEqual(guideLens(refs, "name: IExample\ndata_elements: []\n", "interfaces"), page);
  assert.deepEqual(guideLens(refs, "name: IExample\nkind: no_such_kind\n", "interfaces"), page);
  assert.deepEqual(
    guideLens(refs, "name: IExample\nkind: sender_receiver\nkind: client_server\n", "interfaces"),
    page,
  );
  // A member's indented `kind:` is not the element's.
  assert.deepEqual(
    guideLens(refs, "name: IExample\nmembers:\n  - kind: sender_receiver\n", "interfaces"),
    page,
  );
});

test("no lens: a file without a single identity line, or a layer the table does not know", () => {
  const refs = shipped();
  assert.equal(guideLens(refs, "kind: sender_receiver\n", "interfaces"), undefined);
  assert.equal(
    guideLens(refs, "name: A\nname: B\nkind: sender_receiver\n", "interfaces"),
    undefined,
  );
  assert.equal(guideLens(refs, "", "swc"), undefined);
  // A requirement's identity is `id`, never `name`.
  assert.equal(guideLens(refs, "name: REQ_X\n", "requirements"), undefined);
  assert.equal(guideLens(refs, basicFile("interfaces", "IExample.yaml"), "nowhere"), undefined);
  assert.equal(guideLens(refs, "name: X\n", "toString"), undefined);
});

test("the guide lens raises swcraft.openGuide with its target as the one argument", () => {
  const target = { page: "reference/model/interfaces", anchor: "sender_receiver" };
  assert.deepEqual(guideLensCommand(target), {
    title: "guide",
    command: "swcraft.openGuide",
    arguments: [target],
  });
});

test("every shipped layer's and kind's lens target is accepted by swcraft.openGuide", () => {
  const refs = shipped();
  for (const [layer, facts] of refs.layers) {
    const [pageOnly] = guideLensCommand({ page: facts.guidePage }).arguments;
    assert.deepEqual(
      guideCommandArg(pageOnly),
      { page: sectionPagePath(facts.guidePage), anchor: undefined },
      layer,
    );
    for (const entry of facts.topLevelKinds) {
      const [target] = guideLensCommand({
        page: facts.guidePage,
        anchor: entry.guideAnchor,
      }).arguments;
      assert.deepEqual(
        guideCommandArg(target),
        { page: sectionPagePath(facts.guidePage), anchor: entry.guideAnchor },
        `${layer} ${entry.kind}`,
      );
    }
  }
});

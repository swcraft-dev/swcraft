# `fixtures/compositions/model` — a throwaway model root for compositions

A committed model tree used **in place**, on the precedent of
`fixtures/init_values/model` and `fixtures/access_points/model`:
no committed `.arxml` is generated from it and
`conftest.GOLDEN_MODEL_ROOTS` does not name it. It exists so the composition
emitters have an oracle the two populated goldens cannot be — both of those are
flat single-level projects with one connector each.

What every element in it is for:

| Element | Why it is here |
|---|---|
| `compositions/Root.yaml` | the root (`root: true`); **both** its members are compositions, so the connector at this level joins two composition-typed endpoints — a top-level shape a tool export can have |
| `compositions/Sub.yaml` | the second level: two boundary ports (one provide, one require), three members and two delegations |
| `compositions/Sink.yaml` | the third composition, so the root has two composition members; one boundary port, one member, the third delegation |
| `Sub`'s member `Alpha` of type `Beta` | the member whose **name differs from its type**. It is what makes the two reference bases observable: `CONTEXT-COMPONENT-REF` ends at the *prototype* (`.../Sub/Alpha`) and `TARGET-*-PORT-REF` at the *type's* port (`.../Beta/...`). A tool export can carry such members, for example ones differing from their type in letter case only |
| `Sub`'s member `Ext` of type `ExternalStub` | the external stub. `swc/external/ExternalStub.yaml` carries `external: true` with a verbatim `external_ref` and `external_dest`; the writer emits the prototype with those two strings and **no** `APPLICATION-SW-COMPONENT-TYPE`. It declares no port, so nothing can be wired to it |
| the three delegations | one of the **P** variant (`Sub_pSignalOut_Alpha`, through the differing-name member) and two of the **R** variant (`Sub_rTrigger_Gamma`, `Sink_rSignalIn_Consumer`), so both `INNER-PORT-IREF` bodies are emitted |
| `connectors/root_link.yaml` | the root's assembly connector; it declares **no** `composition`, which means the root |
| `connectors/sub_link.yaml` | the second level's assembly connector; it declares `composition: Sub`, and its `from` endpoint names the type `Beta` rather than the member `Alpha` |

The stub's `external_dest` is `APPLICATION-SW-COMPONENT-TYPE`, the value most of
the export's stubs carry. The other value the export carries,
`COMPOSITION-SW-COMPONENT-TYPE`, is the **importer's** to exercise
the writer copies the string it is given and does not read it.

`requirements/REQ_COMPOSITIONS_001.yaml` is satisfied by every component, the stub included. The requirement's own `description` still says `check-model` warns about each declared component that satisfies none: it predates contract `12.0.0`, which removed that warning, and is kept unchanged as known-stale test data: the RST column of `test_diagram_switch_byte_identity.py`'s record was measured over it. That column is history no case reads since the RST generator left, so no byte-identity record holds the description any more.

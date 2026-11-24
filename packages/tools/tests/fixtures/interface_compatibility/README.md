# `fixtures/interface_compatibility/model` — the compatible-but-different root

A committed model tree used **in place**, on the precedent of
`fixtures/compositions/model` and `fixtures/access_points/model`:
no committed `.arxml` is generated from it and
`conftest.GOLDEN_MODEL_ROOTS` does not name it.

It exists to carry the shape the widened compatibility condition makes legal: a
connector whose two ports name **different** interfaces that the standard nevertheless
joins. `fixtures/compatible_interfaces/model`, the hierarchy built as a second root
(its README), carries it too; every other committed model pairs one interface with
itself, so on all of them the same-name short-circuit answers before the predicate is
asked, and without these two roots the widened condition would be exercised by nothing.

| Element | Why it is here |
|---|---|
| `interfaces/IStatusFull.yaml`, `interfaces/IStatusBasic.yaml` | the sender-receiver pair: the required interface's two data elements are a **subset by name** of the provided interface's three (`[constr_1069]` 1(a)) |
| `interfaces/ICommandFull.yaml`, `interfaces/ICommandBasic.yaml` | the client-server pair: two of three operations, and `Reset`'s one argument differs in **name** while agreeing in direction and resolved type — the pair `[constr_1076]` calls compatible and `[constr_1079]` 1(a) accepts |
| `data_types/Count.yaml`, `data_types/Tally.yaml` | one structure under two names, both resting on `u8_base`: what makes the element and argument types compatible **after resolution** and incomparable by name (`[constr_1050]`) |
| `swc/core/*.yaml` | four components, one port each, so each pair has a provide side and a require side and nothing else is wired |
| `connectors/status_link.yaml`, `connectors/command_link.yaml` | the two pairs, each named by a connector that declares no composition and therefore belongs to the root |
| `compositions/Root.yaml` | the one composition; every declared component is placed exactly once |
| `hardware/`, `requirements/` | what `check-model` requires of any model — one processor — and a requirement every component satisfies, so it is no `trace.requirement-unallocated` warning (the links once kept each component clear of `cross-layer.swc-without-requirement-link`, removed in contract `12.0.0`). The requirement's own `description` still says `check-model` warns about each declared component that satisfies none: it predates contract `12.0.0`, which removed that warning, and is kept unchanged as known-stale test data: the RST column of `test_diagram_switch_byte_identity.py`'s record was measured over it. That column is history no case reads since the RST generator left, so no byte-identity record holds the description any more. The root is **clean** under `check_cross_layer_consistency`, which is what makes "this connector mints nothing" assertable |

The incompatible cases are **perturbations** of this root, written by the tests
that need them (`test_validator_compositions.py`), exactly as the composition
suite perturbs `fixtures/compositions/model`: a clean base plus a one-element
edit is what makes "this rule and nothing else" a checkable claim.

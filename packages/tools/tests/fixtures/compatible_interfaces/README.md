# `fixtures/compatible_interfaces/model` — the XSD-evidence root for compatible interfaces

A committed model tree used **in place**, on the precedent of
`fixtures/compositions/model`, `fixtures/access_points/model`
and `fixtures/interface_compatibility/model`: no committed `.arxml` is generated from it and
`conftest.GOLDEN_MODEL_ROOTS` does not name it.

It exists because the **writer's** delegation refusal was widened, and the
shape that widening newly accepts had no committed model at all: a
`DELEGATION-SW-CONNECTOR` whose two ports name **different** interfaces that the
standard nevertheless joins. The XSD evidence for that shape — generated at
4.2.2 and at 4.3.0 and validated against the licensed schemas — is
`TestTheCompatibleInterfaceRootConforms` in `test_arxml_xsd_conformance.py`.

**Why it is a second root and not an extension of
`fixtures/interface_compatibility/model`.** That one is the *validator's*
perturbation base: flat, one composition, no boundary port and no delegation,
and its README defines it as the clean base seven committed one-element
perturbations rest on. Giving it a hierarchy would change the base those cases
are asserted against. This root carries the hierarchy instead, and carries all
four compatible-but-different shapes at once.

**Every file the two roots share is byte-identical**, so `diff -r` between them
shows exactly the hierarchy this one exists for and nothing else:
`compositions/Edge.yaml`, `swc/core/Relay.yaml`, `Root.yaml`'s extra member and
the requirement that links the fifth component. A description reworded in one
tree alone is a difference that hides the structural ones — eight of them did,
until they were found — so a shared file is edited in both trees
or in neither. `test_the_two_compatible_interface_roots_differ_only_in_the_hierarchy`
in `test_arxml_compositions_writer.py` is the gate.

| Element | Why it is here |
|---|---|
| `interfaces/IStatusFull.yaml`, `interfaces/IStatusBasic.yaml` | the sender-receiver pair: two of three data elements, written in the other type spelling |
| `interfaces/ICommandFull.yaml`, `interfaces/ICommandBasic.yaml` | the client-server pair: two of three operations, and `Reset`'s one argument differs in **name** while agreeing in direction and resolved type (`[constr_1076]`) |
| `data_types/Count.yaml`, `data_types/Tally.yaml` | one structure under two names, both resting on `u8_base`: what makes every pair compatible **after resolution** and incomparable by name (`[constr_1050]`) |
| `connectors/status_link.yaml` | the **sender-receiver assembly connector** the standard joins: required `IStatusBasic` is a subset by name of provided `IStatusFull` (`[constr_1069]` 1(a)) |
| `connectors/command_link.yaml` | the **client-server assembly connector**: required `ICommandBasic` within provided `ICommandFull` (`[constr_1079]` 1(a)) |
| `compositions/Edge.yaml` | the two delegations, one per clause. `pStatusEdge` is a **provide** delegation, so the outer port's direction selects `[constr_1070]` 2(a) — the inner `IStatusFull` and the outer `IStatusBasic` share `speed` and `level`, and one shared compatible member is the whole clause. `rCommandEdge` is a **require** delegation, so it selects `[constr_1080]` 1(a) — the inner `ICommandBasic` is a subset by name of the outer `ICommandFull` |
| `swc/core/Relay.yaml` | `Edge`'s one member, carrying the inner port of each delegation |
| `swc/core/Sensor.yaml`, `Display.yaml`, `Actuator.yaml`, `Controller.yaml` | the two connector pairs, one port each, so each pair has one provide side and one require side |
| `compositions/Root.yaml` | the root: the four connected components and `Edge` as a composition-typed member, so one document carries two `ASSEMBLY-SW-CONNECTOR`s and two `DELEGATION-SW-CONNECTOR`s |
| `hardware/`, `requirements/` | what `check-model` requires of any model — one processor — and a requirement every component satisfies, so it is no `trace.requirement-unallocated` warning (the links once kept each component clear of `cross-layer.swc-without-requirement-link`, removed in contract `12.0.0`). The requirement's own `description` still says `check-model` warns about each declared component that satisfies none: it predates contract `12.0.0`, which removed that warning, and is kept unchanged as known-stale test data: the RST column of `test_diagram_switch_byte_identity.py`'s record was measured over it. That column is history no case reads since the RST generator left, so no byte-identity record holds the description any more. The root is **clean** under `check_cross_layer_consistency`, which is what makes the conformance evidence evidence about the accepted shape and not about a broken model |

Every one of the four pairs names two interfaces, so on this root the same-name
short-circuit answers for none of them and the widened condition is what decides
each. `Edge`'s boundary ports are deliberately not reached by any connector: a
delegation is legal without one, and leaving them free keeps each delegation's
verdict a function of its own two ports.

"""The model passport pass (``swcraft_tools/passport.py``) against two models whose shape is known before the run.

- **The recipe model** (``agent_recipe_support.build_recipe_model``): every field outside
  ``identifiers`` is written out by hand from that module's docstring and its ``EDITS``; the comment
  beside a value says where it comes from.
- **The small specimen** (``passport-small``): its passport is held to the committed source passport
  on every leaf, and the declared levels, the distributions and the locator's answers to an
  independent reading of its YAML files, never to the kernel the pass reads through nor to a name the
  generator chose.

Beside them: the output validates against the passport schema with its keys in the schema's order;
the names-free property, three ways; two copies of one model under differently named directories
give byte-equal passports; and the locator's answers.

What this does not reach: a model shape neither the specimens nor the recipe model hold (a stub
requirement, an element-form link, a hub on a real model); that two generations of one passport
agree (tested where the generator is built); and the other specimens' passports, held by
``test_specimens.py``. The sentinel model is ``passport-small`` rewritten by
``sentinel_support.rewrite_with_sentinels``, whose completeness check is held here both ways.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

import agent_recipe_support
import pytest
import sentinel_support
import yaml
from conftest import AGENT_TASK_BENCH, copy_specimen, specimen_passport
from jsonschema import Draft202012Validator
from passport_support import leaves_differing
from sentinel_support import NAME_SENTINEL, TEXT_SENTINEL, SentinelIncomplete, rewrite_with_sentinels
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import load_model_state
from swcraft_tools.passport import LARGEST, PASSPORT_VERSION, TOP, Locator, compute_passport, schema_text
from swcraft_tools.yaml_load import load_yaml_document

#: The specimen this module reads: two hubs, links, two rated cores, unconnected ports, idle tasks
#: and every requirement case.
SMALL = "passport-small"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(schema_text())


@pytest.fixture(scope="module")
def recipe_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    manifest = agent_recipe_support.build_recipe_model(tmp_path_factory.mktemp("recipe") / "project")
    return manifest.parent / "model"


@pytest.fixture(scope="module")
def small_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The model root of a copy of ``passport-small``."""
    return copy_specimen(SMALL, tmp_path_factory.mktemp("small") / SMALL) / "model"


def _documents(model_root: Path) -> list[tuple[str, dict[str, Any]]]:
    """``(model-root-relative POSIX path, document)`` per model file, by ``yaml.safe_load``, not the kernel."""
    return [
        (path.relative_to(model_root).as_posix(), yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in sorted(model_root.rglob("*.yaml"), key=lambda p: p.as_posix())
    ]


def _components(model_root: Path) -> dict[str, dict[str, Any]]:
    return {doc["name"]: doc for rel, doc in _documents(model_root) if rel.startswith("swc/")}


def _independent_identifiers(model_root: Path) -> dict[str, Any]:
    """The ``identifiers`` section from the raw files: storage directory per layer, ``name`` or ``id`` per element."""
    registry = current_registry()
    directories = {name: registry.storage(name).directory for name in registry.layer_names}
    lengths: dict[str, list[int]] = {name: [] for name in registry.layer_names}
    for rel, doc in _documents(model_root):
        parent = rel.rsplit("/", 1)[0]
        (layer,) = [
            name
            for name, directory in directories.items()
            if parent == directory or (name == "swc" and parent.startswith(f"{directory}/"))
        ]
        lengths[layer].append(len(str(doc.get("id", doc.get("name")))))
    section: dict[str, Any] = {"max_length": max(max(v, default=0) for v in lengths.values())}
    for layer, values in lengths.items():
        section[layer] = {
            "max_length": max(values, default=0),
            "mean_length": round(statistics.fmean(values), 3) if values else 0,
        }
    return section


def _key_paths(document: Any, schema_node: dict[str, Any], schema: dict[str, Any], at: str = "") -> list[str]:
    """Every object's keys, in order, beside the schema's ``properties`` order; mismatches named."""
    problems: list[str] = []
    if "$ref" in schema_node:
        schema_node = schema["$defs"][schema_node["$ref"].rsplit("/", 1)[1]]
    if isinstance(document, dict):
        order = list(schema_node["properties"])
        if list(document) != [key for key in order if key in document]:
            problems.append(f"{at or '/'}: {list(document)} is not in the schema's order {order}")
        for key, value in document.items():
            problems += _key_paths(value, schema_node["properties"][key], schema, f"{at}/{key}")
    return problems


def _leaves(document: Any, at: str = "") -> list[tuple[str, Any]]:
    if isinstance(document, dict):
        return [leaf for key, value in document.items() for leaf in _leaves(value, f"{at}/{key}")]
    if isinstance(document, list):
        return [leaf for index, value in enumerate(document) for leaf in _leaves(value, f"{at}/{index}")]
    return [(at, document)]


class TestTheRecipeModel:
    """Every value outside ``identifiers``, derived by hand from ``agent_recipe_support``."""

    def test_every_field(self, recipe_root: Path) -> None:
        passport = compute_passport(recipe_root).passport
        assert passport["passport_version"] == PASSPORT_VERSION
        assert passport["contract_version"] == current_registry().contract_version
        assert passport["layers"] == {
            # ExampleSWC, ExampleConsumerSWC, LocalConsumerSWC, UnplacedSWC.
            "swc": 4,
            # IExample, IPhysicalSpeed and IOther (sender-receiver), IDiagnosticService (client-server).
            "interfaces": 4,
            # Example_to_Consumer, Physical_to_Consumer (the seed's), Example_to_Local.
            "connectors": 3,
            "application_types": 1,
            "implementation_types": 12,
            "base_types": 12,
            # REQ_EXAMPLE_001, 002, 003.
            "requirements": 3,
            # Root and ConsumerGroup.
            "compositions": 2,
            # Task_10ms and Task_10ms_P1.
            "tasks": 2,
            "sequences": 1,
            # CPU, CORE0, CORE1, PARTITION0, PARTITION1, RAM0.
            "hardware": 6,
            "mapping_sets": 1,
            "units": 0,
            "compu_methods": 0,
            "data_constraints": 0,
        }
        assert passport["components"] == {
            # Three on ExampleSWC (its spare rSpare among them); two each on ExampleConsumerSWC and
            # UnplacedSWC; one on LocalConsumerSWC.
            "ports": {"total": 8, "top": [3, 2, 2, 1]},
            # The median is 2, so a hub would need more than 4.
            "hubs": [],
            # UnplacedSWC's rOther and pOther, and ExampleSWC's rSpare; the consumer's ports are
            # reached by the delegations.
            "unconnected": {"total": 3, "max_on_one": 2},
            # D twice (DECLARED_LEVELS), B(D) counted as B; the consumer declares none.
            "by_declared_level": {"QM": 0, "A": 0, "B": 1, "C": 0, "D": 2},
            # ExampleRun and ExampleSlowRun, ExampleConsumerRun, LocalRun, OtherRun.
            "runnables": 5,
        }
        assert passport["interfaces"] == {
            "sender_receiver": 3,
            "client_server": 1,
            # IExample: pOut, rIn, rLocal; IOther: rOther, pOther, rSpare; IPhysicalSpeed: pPhysical,
            # rPhysical.
            "fan_out": {"max": 3, "top": [3, 3, 2]},
        }
        # ConsumerGroup's two delegations; no pair of non-hub components is exceeded.
        assert passport["connectors"] == {"assembly": 3, "delegation": 2, "links": 0}
        # No partition of the seed or the partitioned vectors declares a level.
        assert passport["hardware"] == {"cores": 2, "partitions": 2, "memories": 1, "rated_partitions": 0}
        assert passport["tasks"] == {"idle": 0}
        assert passport["requirements"] == {
            "on_all_site_kinds": 0,
            # REQ_EXAMPLE_002 is on a port and an interface, so not on one port alone.
            "on_one_port": 0,
            # REQ_EXAMPLE_003.
            "unallocated": 1,
            # rLocal's REQ_EXAMPLE_002@1, the requirement at version 2.
            "stale_links": 1,
            # REQ_EXAMPLE_001 on the four components, REQ_EXAMPLE_002 on rLocal and on IOther.
            "links": 6,
        }

    def test_the_identifiers_equal_an_independent_reading(self, recipe_root: Path) -> None:
        assert compute_passport(recipe_root).passport["identifiers"] == _independent_identifiers(recipe_root)

    def test_the_locator(self, recipe_root: Path) -> None:
        locator = compute_passport(recipe_root).locator
        # ExampleSWC holds three ports; the two that hold two are ordered by name.
        assert locator.largest_component() == "ExampleSWC"
        assert locator.largest_components() == ("ExampleSWC", "ExampleConsumerSWC", "UnplacedSWC", "LocalConsumerSWC")
        assert locator.port_count("LocalConsumerSWC") == 1
        assert locator.heaviest_interface() == "IExample"
        assert locator.ports_naming("IExample") == (
            ("ExampleConsumerSWC", "rIn"),
            ("ExampleSWC", "pOut"),
            ("LocalConsumerSWC", "rLocal"),
        )
        assert locator.ports_naming("IDiagnosticService") == ()
        # IExample and IOther (three ports each) by name, then IPhysicalSpeed; the client-server
        # interface is never one, whatever names it.
        assert locator.heaviest_sender_receiver_interfaces() == ("IExample", "IOther", "IPhysicalSpeed")
        assert locator.most_unconnected_component() == "UnplacedSWC"
        assert locator.unconnected_ports("UnplacedSWC") == ("rOther", "pOther")
        assert locator.unconnected_ports("ExampleSWC") == ("rSpare",)
        assert locator.unconnected_ports("ExampleConsumerSWC") == ()
        assert locator.unconnected_pairs() == (
            ("ExampleSWC", "rSpare"),
            ("UnplacedSWC", "pOther"),
            ("UnplacedSWC", "rOther"),
        )

    @pytest.mark.parametrize(
        ("question", "says"),
        [
            ("largest_component", "the model has no component"),
            ("heaviest_interface", "no component port names an interface"),
            ("most_unconnected_component", "the model has no unconnected port"),
        ],
    )
    def test_a_question_the_model_cannot_answer_is_a_lookup_error_saying_so(self, question: str, says: str) -> None:
        # No component at all for the first; for the others, one whose one port is connected and
        # names no interface.
        ports = {} if question == "largest_component" else {"Only": ("p",)}
        unconnected = {name: () for name in ports}
        locator = Locator(_ports=ports, _unconnected=unconnected, _naming={}, _sender_receiver=frozenset())
        with pytest.raises(LookupError, match=f"^{says}$"):
            getattr(locator, question)()


class TestTheSmallSpecimen:
    """``passport-small``'s passport is its committed source, and its figures equal an independent reading."""

    def test_the_passport_is_its_source_passport(self, small_root: Path) -> None:
        passport = compute_passport(small_root).passport
        assert leaves_differing(specimen_passport(SMALL), passport) == []

    def test_the_declared_levels_equal_an_independent_reading(self, small_root: Path) -> None:
        components = compute_passport(small_root).passport["components"]
        declared = Counter(doc["asil"] for doc in _components(small_root).values())
        # Every component declares a level, as the source passport states.
        assert sum(declared.values()) == specimen_passport(SMALL)["layers"]["swc"]
        assert components["by_declared_level"] == {
            level: declared.get(level, 0) for level in ("QM", "A", "B", "C", "D")
        }

    def test_the_distributions_equal_an_independent_reading(self, small_root: Path) -> None:
        passport = compute_passport(small_root).passport
        components = _components(small_root)
        port_counts = sorted((len(doc["ports"]) for doc in components.values()), reverse=True)
        assert passport["components"]["ports"]["top"] == port_counts[:TOP]
        naming = Counter(port["interface"] for doc in components.values() for port in doc["ports"])
        fan_out = sorted(naming.values(), reverse=True)[:TOP]
        assert passport["interfaces"]["fan_out"] == {"max": fan_out[0], "top": fan_out}
        assert passport["identifiers"] == _independent_identifiers(small_root)

    def test_the_locator(self, small_root: Path) -> None:
        locator = compute_passport(small_root).locator
        components = _components(small_root)
        counts = {name: len(doc["ports"]) for name, doc in components.items()}
        ranked = tuple(sorted(counts, key=lambda name: (-counts[name], name)))
        hubs = specimen_passport(SMALL)["components"]["hubs"]
        # The component with the most ports, and the second, are the source passport's two hubs.
        assert locator.largest_component() == ranked[0]
        assert counts[ranked[0]] == hubs[0]
        assert locator.largest_components() == ranked[:LARGEST]
        assert len(locator.largest_components()) == LARGEST
        assert locator.port_count(ranked[1]) == counts[ranked[1]] == hubs[1]
        naming: dict[str, list[tuple[str, str]]] = {}
        for name, doc in components.items():
            for port in doc["ports"]:
                naming.setdefault(port["interface"], []).append((name, port["name"]))
        heaviest = sorted(naming, key=lambda name: (-len(naming[name]), name))[0]
        assert locator.heaviest_interface() == heaviest
        assert locator.ports_naming(heaviest) == tuple(sorted(naming[heaviest]))
        sender_receiver = {
            doc["name"]
            for rel, doc in _documents(small_root)
            if rel.startswith("interfaces/") and doc["kind"] == "sender_receiver"
        }
        named = {name: pairs for name, pairs in naming.items() if name in sender_receiver}
        assert locator.heaviest_sender_receiver_interfaces() == tuple(
            sorted(named, key=lambda name: (-len(named[name]), name))
        )
        connectors = [doc for rel, doc in _documents(small_root) if rel.startswith("connectors/")]
        # A port is unconnected when no connector end reaches it; the source passport states no
        # delegation, so the connector files are every end there is.
        assert specimen_passport(SMALL)["connectors"]["delegation"] == 0
        reached = {(end["swc"], end["port"]) for doc in connectors for end in (doc["from"], doc["to"]) if "swc" in end}
        unconnected = {
            name: tuple(port["name"] for port in doc["ports"] if (name, port["name"]) not in reached)
            for name, doc in components.items()
        }
        assert unconnected and any(unconnected.values())
        assert {name: locator.unconnected_ports(name) for name in components} == unconnected
        assert locator.unconnected_pairs() == tuple(sorted((n, p) for n, ports in unconnected.items() for p in ports))
        most = sorted((name for name in unconnected if unconnected[name]), key=lambda n: (-len(unconnected[n]), n))
        assert locator.most_unconnected_component() == most[0]
        # The connectors with an end on each component, read off the connector files; one with both
        # ends on a component counts once.
        ends = Counter(
            name for doc in connectors for name in {doc["from"].get("swc"), doc["to"].get("swc")} if name is not None
        )
        assert {name: locator.connector_count(name) for name in components} == {
            name: ends.get(name, 0) for name in components
        }
        assert locator.connector_count(ranked[0]) > 0
        assert locator.connector_count("NoSuchComponent") == 0


class TestTheSchemaAndTheOrder:
    @pytest.mark.parametrize("model", ["recipe_root", "small_root"])
    def test_the_passport_validates_with_its_keys_in_the_schemas_order(
        self, model: str, schema: dict[str, Any], request: pytest.FixtureRequest
    ) -> None:
        passport = compute_passport(request.getfixturevalue(model)).passport
        Draft202012Validator(schema).validate(passport)
        assert passport["passport_version"] == schema["x-passport-version"]
        assert _key_paths(passport, schema, schema) == []

    @pytest.mark.parametrize("model", ["recipe_root", "small_root"])
    def test_every_list_is_descending_and_every_maximum_its_head(
        self, model: str, request: pytest.FixtureRequest
    ) -> None:
        passport = compute_passport(request.getfixturevalue(model)).passport
        components = passport["components"]
        for values in (components["ports"]["top"], components["hubs"], passport["interfaces"]["fan_out"]["top"]):
            assert values == sorted(values, reverse=True)
        top = passport["interfaces"]["fan_out"]["top"]
        assert passport["interfaces"]["fan_out"]["max"] == (top[0] if top else 0)
        assert components["ports"]["total"] >= sum(components["ports"]["top"])
        assert components["unconnected"]["total"] >= components["unconnected"]["max_on_one"]

    def test_the_schema_refuses_a_string_in_a_counts_place(self, small_root: Path, schema: dict[str, Any]) -> None:
        passport = compute_passport(small_root).passport
        passport["components"]["unconnected"]["total"] = str(passport["components"]["unconnected"]["total"])
        errors = list(Draft202012Validator(schema).iter_errors(passport))
        assert [list(error.absolute_path) for error in errors] == [["components", "unconnected", "total"]]


@pytest.fixture(scope="module")
def sentinel_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The model root of a copy of ``passport-small`` with a sentinel in every name and every text.

    The rewrite (``sentinel_support.rewrite_with_sentinels``) fails here if its completeness check
    finds a name it missed."""
    root = copy_specimen(SMALL, tmp_path_factory.mktemp("sentinel") / SMALL) / "model"
    rewrite_with_sentinels(root)
    return root


class TestNamesFree:
    """No sentinel leaks, every leaf is numeric, and a string in a count's place is refused."""

    def test_the_model_carries_the_sentinels(self, sentinel_root: Path) -> None:
        registry = current_registry()
        state = load_model_state(sentinel_root)
        identities = [
            str(getattr(element, registry.layer(layer).identity_field))
            for layer in registry.layer_names
            for element in state[layer]
        ]
        assert identities and all(NAME_SENTINEL in identity for identity in identities)
        texts = [element.description for layer in ("swc", "interfaces", "requirements") for element in state[layer]]
        assert texts and all(TEXT_SENTINEL in text for text in texts)

    def test_no_sentinel_reaches_the_passport(self, sentinel_root: Path) -> None:
        result = compute_passport(sentinel_root)
        text = json.dumps(result.passport)
        assert "ZqSentinel" not in text
        assert "ZqSentinel" not in repr(result.locator)
        assert repr(result.locator) == "Locator()"

    def test_every_leaf_but_the_two_versions_is_numeric(self, sentinel_root: Path) -> None:
        leaves = _leaves(compute_passport(sentinel_root).passport)
        strings = sorted(at for at, value in leaves if isinstance(value, str))
        assert strings == ["/contract_version", "/passport_version"]
        assert all(
            isinstance(value, int | float) and not isinstance(value, bool) for at, value in leaves if at not in strings
        )

    def test_the_sentinel_model_has_the_plain_models_shape(self, sentinel_root: Path, small_root: Path) -> None:
        sentinel = compute_passport(sentinel_root).passport
        plain = compute_passport(small_root).passport
        # The sentinels lengthen the identifiers and change nothing else.
        assert sentinel.pop("identifiers") != plain.pop("identifiers")
        assert sentinel == plain

    def test_the_completeness_check_finds_nothing_on_the_sentinel_model(self, sentinel_root: Path) -> None:
        assert sentinel_support.unsentinelled_fields(sentinel_root) == []

    def test_the_sentinel_model_resolves_what_the_plain_one_resolves(self, sentinel_root: Path, small_root: Path):
        assert sentinel_support.unresolved_references(sentinel_root) == sentinel_support.unresolved_references(
            small_root
        )

    @pytest.mark.parametrize(
        ("skipped", "fields"),
        [
            ("identity", ["swc.port.name"]),
            ("reference", ["swc.port.interface", "swc.port.interface (unresolved)"]),
        ],
        ids=["a-member-identity", "a-reference"],
    )
    def test_the_completeness_check_fails_a_rewrite_whose_walk_skips_one_field(
        self, skipped: str, fields: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A walk made to skip one field: the completeness check names that field alone, and a dangling reference."""
        if skipped == "identity":
            member_identity = sentinel_support._member_identity

            def without_port_names(kind: Any, name: str) -> frozenset[str]:
                found = member_identity(kind, name)
                return found - {"name"} if (kind.id, name) == ("kind:swc.swc", "ports") else found

            monkeypatch.setattr(sentinel_support, "_member_identity", without_port_names)
        else:
            names_of = sentinel_support.names_of

            def without_port_interfaces(kind: Any, registry: Any) -> frozenset[str]:
                found = names_of(kind, registry)
                return found - {"interface"} if kind.id == "kind:swc.port" else found

            monkeypatch.setattr(sentinel_support, "names_of", without_port_interfaces)
        root = copy_specimen(SMALL, tmp_path / SMALL) / "model"
        with pytest.raises(SentinelIncomplete) as raised:
            rewrite_with_sentinels(root)
        assert raised.value.fields == fields

    def test_a_nested_instance_selector_is_prefixed_segment_by_segment(self, tmp_path: Path) -> None:
        """A planted nested ``instance`` path ``Nested/Leaf`` gains the sentinel segment by segment and still walks."""
        root = _with_a_nested_instance(copy_specimen(SMALL, tmp_path / SMALL) / "model")
        rewrite_with_sentinels(root)
        state = load_model_state(root)
        expected = f"{NAME_SENTINEL}Nested/{NAME_SENTINEL}Leaf"
        task = next(task for task in state["tasks"] if task.name == f"{NAME_SENTINEL}Ts99Nested")
        step = next(sequence for sequence in state["sequences"] if sequence.name == f"{NAME_SENTINEL}Sq99Nested")
        assert [entry.instance for entry in task.runnables] == [expected]
        assert [entry.instance for entry in step.steps] == [expected]
        assert sentinel_support.unresolved_references(root) == {}

    @pytest.mark.parametrize(
        ("broken", "fields"),
        [
            (
                "one-string-prefix",
                [
                    "sequences.sequence_step.instance",
                    "sequences.sequence_step.instance (unresolved)",
                    "tasks.task_runnable_ref.instance",
                    "tasks.task_runnable_ref.instance (unresolved)",
                ],
            ),
            (
                "no-path-fields",
                ["sequences.sequence_step.instance (unresolved)", "tasks.task_runnable_ref.instance (unresolved)"],
            ),
        ],
        ids=["the-walk-prefixes-the-path-whole", "the-path-is-read-as-one-name-throughout"],
    )
    def test_a_path_prefixed_as_one_string_fails_the_check(
        self, broken: str, fields: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A path prefixed as one string walks to no member; the resolution names it even with no segment check."""
        if broken == "one-string-prefix":
            monkeypatch.setattr(sentinel_support, "_prefixed_path", sentinel_support._prefixed)
        else:
            monkeypatch.setattr(sentinel_support, "_is_instance_path", lambda kind, key: False)
        root = _with_a_nested_instance(copy_specimen(SMALL, tmp_path / SMALL) / "model")
        with pytest.raises(SentinelIncomplete) as raised:
            rewrite_with_sentinels(root)
        assert raised.value.fields == fields

    def test_an_element_form_derived_from_is_prefixed_in_its_name_alone(self, tmp_path: Path) -> None:
        """A planted element-form ``derived_from`` keeps its layer token and prefixes its name; both forms resolve."""
        root, component, parent = _with_a_derived_from(copy_specimen(SMALL, tmp_path / SMALL) / "model")
        rewrite_with_sentinels(root)
        derived = [requirement.derived_from for requirement in load_model_state(root)["requirements"]]
        assert [f"swc:{NAME_SENTINEL}{component}", f"{NAME_SENTINEL}{parent}"] in derived
        assert sentinel_support.unresolved_references(root) == {}

    def test_an_element_form_prefixed_as_one_string_fails_both_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ZqSentinelNameswc:Controller`` fails both the name-part check and the resolution."""
        monkeypatch.setattr(sentinel_support, "_prefixed_element_forms", sentinel_support._prefixed)
        root, _, _ = _with_a_derived_from(copy_specimen(SMALL, tmp_path / SMALL) / "model")
        with pytest.raises(SentinelIncomplete) as raised:
            rewrite_with_sentinels(root)
        assert raised.value.fields == [
            "requirements.requirement.derived_from",
            "requirements.requirement.derived_from (unresolved)",
        ]

    def test_the_three_layers_no_specimen_holds_are_walked_and_checked(self, tmp_path: Path) -> None:
        """A unit, a compu method and a data constraint from the showcase bench are rewritten, leaking nothing."""
        root = copy_specimen(SMALL, tmp_path / SMALL) / "model"
        registry = current_registry()
        for layer, name in PLANTED.items():
            directory = registry.layer(layer).storage.directory
            (root / directory).mkdir(parents=True, exist_ok=True)
            source = BENCH_MODEL / directory / f"{name}.yaml"
            (root / directory / source.name).write_bytes(source.read_bytes())
        rewrite_with_sentinels(root)
        state = load_model_state(root)
        for layer in PLANTED:
            assert [element.name for element in state[layer]] == [f"{NAME_SENTINEL}{PLANTED[layer]}"]
        compu = state["compu_methods"][0]
        assert compu.entries and all(entry.name.startswith(NAME_SENTINEL) for entry in compu.entries)
        result = compute_passport(root)
        assert "ZqSentinel" not in json.dumps(result.passport) and repr(result.locator) == "Locator()"


#: One element of each layer no specimen holds, from the committed showcase bench: layer -> name.
BENCH_MODEL = AGENT_TASK_BENCH / "model"
PLANTED = {
    "units": "KilometerPerHour",
    "compu_methods": "VehicleOperatingState_Values",
    "data_constraints": "VehicleSpeed_Constr",
}


def _write_yaml(path: Path, document: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _with_a_derived_from(root: Path) -> tuple[Path, str, str]:
    """A copy of ``passport-small``'s model under *root* with one requirement deriving from two names.

    Its first requirement derives from the first component (``swc:<name>``) and the second requirement (``<id>``);
    returns *root* and those two names."""
    state = load_model_state(root)
    component = state["swc"][0].name
    parent = sorted(requirement.id for requirement in state["requirements"])[1]
    path = sorted((root / "requirements").glob("*.yaml"), key=lambda path: path.as_posix())[0]
    document = load_yaml_document(path.read_text(encoding="utf-8"))
    assert document["id"] != parent
    document["derived_from"] = [f"swc:{component}", parent]
    _write_yaml(path, document)
    return root, component, parent


def _with_a_nested_instance(root: Path) -> Path:
    """*root*, a copy of ``passport-small``'s model, with ``Nested/Leaf`` named by ``Ts99Nested`` and ``Sq99Nested``."""
    compositions = root / "compositions"
    root_file = compositions / "Root.yaml"
    document = load_yaml_document(root_file.read_text(encoding="utf-8"))
    component = document["members"][0]["type"]
    document["members"].append({"name": "Nested", "composition": "Nested"})
    _write_yaml(root_file, document)
    _write_yaml(
        compositions / "Nested.yaml",
        {"name": "Nested", "title": "Nested", "ports": [], "members": [{"name": "Leaf", "type": component}]},
    )
    entry = {"instance": "Nested/Leaf", "runnable": "R1"}
    partition = load_yaml_document(next((root / "tasks").glob("*.yaml")).read_text(encoding="utf-8"))["partition"]
    _write_yaml(
        root / "tasks" / "Ts99Nested.yaml",
        {"name": "Ts99Nested", "period": 0.01, "priority": 1, "partition": partition, "runnables": [entry]},
    )
    _write_yaml(
        root / "sequences" / "Sq99Nested.yaml",
        {"name": "Sq99Nested", "task": "Ts99Nested", "title": "", "steps": [entry]},
    )
    return root


def test_two_copies_under_differently_named_directories_give_byte_equal_passports(tmp_path: Path) -> None:
    texts = []
    for directory in ("alpha-first", "zz_second"):
        root = copy_specimen(SMALL, tmp_path / directory / SMALL) / "model"
        texts.append(json.dumps(compute_passport(root).passport, indent=2).encode("utf-8"))
    assert texts[0] == texts[1]

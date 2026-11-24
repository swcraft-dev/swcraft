"""The scheduling-identity leaf.

The resolver table's six rows, the path grammar, the walk as paths with exact
counts and the 64 cap, the undefined walk, the uncapped occurrence enumeration,
the sort key, and an ``ast`` pin of what the leaf imports.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from swcraft_tools import scheduling_identity as leaf
from swcraft_tools.schema import Composition

LEAF_SOURCE = Path(leaf.__file__)


def _composition(name: str, members: list[dict], *, root: bool = False) -> Composition:
    return Composition.model_validate({"name": name, "title": name, "root": root, "members": members})


def _root(*members: dict) -> Composition:
    return _composition("Root", list(members), root=True)


def _type(name: str, type_name: str) -> dict:
    return {"name": name, "type": type_name}


def _nested(name: str, composition: str) -> dict:
    return {"name": name, "composition": composition}


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


class TestTheTable:
    """The six rows of the module docstring's table, each on its own model."""

    LAYER = [
        _root(_type("A", "A"), _type("w1", "W"), _type("w2", "W"), _nested("sub", "Sub")),
        _composition("Sub", [_type("inner", "Inner")]),
    ]

    def resolve(self, swc: str | None = None, instance: str | None = None) -> leaf.Resolution:
        return leaf.identities_from(self.LAYER).resolve(swc, instance)

    def test_a_type_placed_once_resolves_to_its_one_instance(self) -> None:
        assert self.resolve(swc="A") == leaf.Resolution(identity=("instance", "A"), type="A")

    def test_a_type_placed_once_through_a_nested_composition_resolves_to_its_path(self) -> None:
        assert self.resolve(swc="Inner") == leaf.Resolution(identity=("instance", "sub/inner"), type="Inner")

    def test_a_type_placed_nowhere_is_the_unplaced_type(self) -> None:
        assert self.resolve(swc="Nowhere") == leaf.Resolution(identity=("unplaced-type", "Nowhere"), type="Nowhere")

    def test_a_type_placed_more_than_once_is_ambiguous_with_its_count_and_paths(self) -> None:
        resolution = self.resolve(swc="W")
        assert resolution.refusal == "instance-ambiguous"
        assert (resolution.count, resolution.paths) == (2, ("w1", "w2"))
        # A refused entry keeps its syntactic triple and its type.
        assert resolution.identity == ("syntactic", "W", None)
        assert resolution.type == "W"

    def test_a_path_to_a_component_member_resolves_to_that_occurrence(self) -> None:
        assert self.resolve(instance="w2") == leaf.Resolution(identity=("instance", "w2"), type="W")
        assert self.resolve(instance="sub/inner") == leaf.Resolution(identity=("instance", "sub/inner"), type="Inner")

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("nope", id="no-such-member"),
            pytest.param("A/inner", id="descends-into-a-type-member"),
            pytest.param("sub/nope", id="no-such-nested-member"),
            pytest.param("Root/A", id="starts-with-the-root-composition-name"),
            pytest.param("sub//inner", id="an-empty-segment"),
            pytest.param("/A", id="a-leading-separator"),
        ],
    )
    def test_a_path_that_does_not_walk_to_a_member_is_unknown(self, path: str) -> None:
        resolution = self.resolve(instance=path)
        assert resolution.refusal == "instance-unknown"
        assert resolution.identity == ("syntactic", None, path)
        assert resolution.type is None

    def test_a_path_through_an_undeclared_composition_is_unknown(self) -> None:
        layer = [_root(_nested("ghost", "Ghost"))]
        assert leaf.identities_from(layer).resolve(None, "ghost/x").refusal == "instance-unknown"

    def test_a_path_ending_on_a_composition_member_is_not_a_component(self) -> None:
        resolution = self.resolve(instance="sub")
        assert resolution.refusal == "instance-not-a-component"
        assert resolution.type is None

    def test_type_of_reads_the_resolved_type(self) -> None:
        identities = leaf.identities_from(self.LAYER)
        assert identities.type_of(None, "sub/inner") == "Inner"
        assert identities.type_of("W", None) == "W"
        assert identities.type_of(None, "nope") is None


class TestTheInstanceTwiceThroughOneComposition:
    def test_left_and_right_worker_are_two_instances_of_one_type(self) -> None:
        layer = [
            _root(_nested("left", "Pair"), _nested("right", "Pair")),
            _composition("Pair", [_type("worker", "Worker")]),
        ]
        identities = leaf.identities_from(layer)
        assert identities.resolve(None, "left/worker").identity == ("instance", "left/worker")
        assert identities.resolve(None, "right/worker").identity == ("instance", "right/worker")
        ambiguous = identities.resolve("Worker", None)
        assert ambiguous.refusal == "instance-ambiguous"
        assert ambiguous.paths == ("left/worker", "right/worker")


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


class TestTheWalk:
    def test_the_counts_are_exact_past_the_cap_and_the_paths_stop_at_64(self) -> None:
        """A diamond of seven levels: the count is exact and the paths kept are the first 64 in walk order.

        2**7 = 128 paths reach `Leaf`."""
        depth = 7
        layer = [_root(_nested("L0", "Level0"))]
        for level in range(depth):
            layer.append(
                _composition(f"Level{level}", [_nested("a", f"Level{level + 1}"), _nested("b", f"Level{level + 1}")])
            )
        layer.append(_composition(f"Level{depth}", [_type("leaf", "Leaf")]))
        placements = leaf.identities_from(layer).placements
        assert placements is not None
        placement = placements["Leaf"]
        assert placement.count == 2**depth
        assert len(placement.paths) == leaf.PATH_CAP == 64
        assert placement.paths[0] == "L0/" + "/".join(["a"] * depth) + "/leaf"
        assert placement.paths[1] == "L0/" + "/".join(["a"] * (depth - 1) + ["b"]) + "/leaf"

    def test_a_deep_diamond_is_walked_once_per_composition(self) -> None:
        """Forty levels: 2**40 paths, which an unmemoised walk would never finish."""
        depth = 40
        layer = [_root(_nested("L0", "Level0"))]
        for level in range(depth - 1):
            layer.append(
                _composition(f"Level{level}", [_nested("a", f"Level{level + 1}"), _nested("b", f"Level{level + 1}")])
            )
        layer.append(_composition(f"Level{depth - 1}", [_type("leaf", "Leaf")]))
        placements = leaf.identities_from(layer).placements
        assert placements is not None
        assert placements["Leaf"].count == 2 ** (depth - 1)
        assert len(placements["Leaf"].paths) == 64

    def test_a_composition_the_root_does_not_reach_places_nothing(self) -> None:
        layer = [_root(_type("A", "A")), _composition("Orphan", [_type("x", "X")])]
        assert leaf.identities_from(layer).placements == {"A": leaf.Placement(1, ("A",))}

    @pytest.mark.parametrize(
        "layer",
        [
            pytest.param([_composition("NotRoot", [_type("A", "A")])], id="no-root"),
            pytest.param([_root(_type("A", "A")), _composition("Other", [_type("B", "B")], root=True)], id="two-roots"),
            pytest.param(
                [_root(_nested("loop", "Loop")), _composition("Loop", [_nested("self", "Loop")])], id="a-reached-cycle"
            ),
        ],
    )
    def test_the_walk_is_undefined_and_every_identity_is_syntactic(self, layer: list[Composition]) -> None:
        identities = leaf.identities_from(layer)
        assert identities.placements is None
        assert not identities.defined
        assert identities.resolve("A", None) == leaf.Resolution(identity=("syntactic", "A", None), type="A")
        # An `instance` entry has no type and is not refused: the composition
        # rules own the finding.
        assert identities.resolve(None, "A") == leaf.Resolution(identity=("syntactic", None, "A"), type=None)

    def test_a_cycle_the_root_does_not_reach_changes_nothing(self) -> None:
        layer = [_root(_type("A", "A")), _composition("Loop", [_nested("self", "Loop")])]
        assert leaf.identities_from(layer).resolve("A", None).identity == ("instance", "A")

    def test_the_first_composition_of_a_name_wins(self) -> None:
        layer = [_root(_nested("sub", "Sub")), _composition("Sub", [_type("x", "X")]), _composition("Sub", [])]
        assert leaf.composition_index(layer)["Sub"].members[0].name == "x"
        assert leaf.identities_from(layer).resolve(None, "sub/x").identity == ("instance", "sub/x")


# ---------------------------------------------------------------------------
# The uncapped occurrence enumeration
# ---------------------------------------------------------------------------


def _diamond(depth: int) -> list[Composition]:
    """The root holds `L0`; each level holds `a` and `b` of the next; the last holds `leaf: Leaf`."""
    layer = [_root(_nested("L0", "Level0"))]
    for level in range(depth):
        layer.append(
            _composition(f"Level{level}", [_nested("a", f"Level{level + 1}"), _nested("b", f"Level{level + 1}")])
        )
    layer.append(_composition(f"Level{depth}", [_type("leaf", "Leaf")]))
    return layer


class TestTheOccurrences:
    def test_a_type_past_the_cap_gets_every_path(self) -> None:
        """70 members of one type: the walk keeps 64 paths, the enumeration all 70."""
        names = [f"w{index:02d}" for index in range(70)]
        identities = leaf.identities_from([_root(*(_type(name, "W") for name in names))])
        assert identities.placements is not None
        assert len(identities.placements["W"].paths) == leaf.PATH_CAP
        assert identities.occurrences() == [(name, "W") for name in names]

    def test_a_diamond_past_the_cap_gets_every_path_in_walk_order(self) -> None:
        depth = 7
        identities = leaf.identities_from(_diamond(depth))
        occurrences = identities.occurrences()
        assert len(occurrences) == 2**depth
        # Hand-written: the first, second and last paths, depth first in member order.
        assert occurrences[0] == ("L0/" + "/".join(["a"] * depth) + "/leaf", "Leaf")
        assert occurrences[1] == ("L0/" + "/".join(["a"] * (depth - 1) + ["b"]) + "/leaf", "Leaf")
        assert occurrences[-1] == ("L0/" + "/".join(["b"] * depth) + "/leaf", "Leaf")
        assert len({path for path, _ in occurrences}) == 2**depth
        # The walk's capped paths are the enumeration's first 64, in the same order.
        assert identities.placements is not None
        assert identities.placements["Leaf"].paths == tuple(path for path, _ in occurrences[: leaf.PATH_CAP])

    def test_nested_compositions_interleave_in_member_order(self) -> None:
        layer = [
            _root(_type("first", "A"), _nested("sub", "Sub"), _type("last", "B"), _nested("ghost", "Ghost")),
            _composition("Sub", [_type("x", "X"), _nested("deeper", "Deeper"), _type("y", "A")]),
            _composition("Deeper", [_type("z", "Z")]),
            _composition("Unreached", [_type("u", "U")]),
        ]
        assert leaf.identities_from(layer).occurrences() == [
            ("first", "A"),
            ("sub/x", "X"),
            ("sub/deeper/z", "Z"),
            ("sub/y", "A"),
            ("last", "B"),
        ]

    @pytest.mark.parametrize(
        "layer",
        [
            pytest.param(_diamond(3), id="diamond"),
            pytest.param(
                [
                    _root(_nested("left", "Pair"), _nested("right", "Pair"), _type("solo", "Solo")),
                    _composition("Pair", [_type("worker", "Worker"), _nested("inner", "Inner")]),
                    _composition("Inner", [_type("leaf", "Leaf"), _type("other", "Worker")]),
                ],
                id="nested",
            ),
        ],
    )
    def test_every_path_resolves_to_its_own_type(self, layer: list[Composition]) -> None:
        identities = leaf.identities_from(layer)
        occurrences = identities.occurrences()
        assert occurrences
        for path, type_name in occurrences:
            assert identities.resolve(None, path) == leaf.Resolution(identity=("instance", path), type=type_name)
        # And the count per type is the walk's exact count.
        assert identities.placements is not None
        counted: dict[str, int] = {}
        for _, type_name in occurrences:
            counted[type_name] = counted.get(type_name, 0) + 1
        assert counted == {name: placement.count for name, placement in identities.placements.items()}

    @pytest.mark.parametrize(
        "layer",
        [
            pytest.param([_composition("NotRoot", [_type("A", "A")])], id="no-root"),
            pytest.param([_root(_type("A", "A")), _composition("Other", [_type("B", "B")], root=True)], id="two-roots"),
            pytest.param(
                [_root(_nested("loop", "Loop")), _composition("Loop", [_nested("self", "Loop")])], id="a-reached-cycle"
            ),
        ],
    )
    def test_an_undefined_walk_has_no_occurrences(self, layer: list[Composition]) -> None:
        assert leaf.identities_from(layer).occurrences() == []

    def test_a_cycle_the_root_does_not_reach_changes_nothing(self) -> None:
        layer = [_root(_type("A", "A")), _composition("Loop", [_nested("self", "Loop")])]
        assert leaf.identities_from(layer).occurrences() == [("A", "A")]

    def test_a_deep_diamond_of_empty_compositions_is_visited_once_per_composition(self) -> None:
        """64 levels of `a`/`b` over an empty bottom: 2**64 paths, no component, no row.

        Without the pruning the enumeration would walk every path and never end;
        the walk itself is memoised and was already linear.
        """
        layer = _diamond(64)
        layer[-1] = _composition("Level64", [_nested("ghost", "Undeclared")])
        identities = leaf.identities_from(layer)
        assert identities.placements == {}
        assert identities.occurrences() == []

    def test_a_deep_empty_diamond_beside_a_real_component_yields_only_the_component(self) -> None:
        empty = _diamond(64)
        empty[-1] = _composition("Level64", [])
        layer = [
            _root(_nested("L0", "Level0"), _type("worker", "Worker"), _nested("sub", "Sub")),
            *empty[1:],
            _composition("Sub", [_nested("again", "Level1"), _type("leaf", "Leaf")]),
        ]
        identities = leaf.identities_from(layer)
        assert identities.occurrences() == [("worker", "Worker"), ("sub/leaf", "Leaf")]


# ---------------------------------------------------------------------------
# The sort key and the leaf's imports
# ---------------------------------------------------------------------------


def test_the_sort_key_orders_none_as_the_empty_string() -> None:
    triples = [("B", None, "R"), (None, "a/b", "R"), ("A", None, "R")]
    assert sorted(triples, key=leaf.identity_sort_key) == [(None, "a/b", "R"), ("A", None, "R"), ("B", None, "R")]
    assert leaf.identity_sort_key(("instance", None)) == ("instance", "")


def test_the_written_selector_is_the_label() -> None:
    assert leaf.written_selector("A", None) == "A"
    assert leaf.written_selector(None, "left/worker") == "left/worker"


def test_the_leaf_imports_the_schema_and_the_composition_reader_and_nothing_else() -> None:
    """No ``validator``, so every importer can reach it without a cycle."""
    tree = ast.parse(LEAF_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("swcraft_tools"):
            imported |= {f"{node.module}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names if alias.name.startswith("swcraft_tools")}
    assert imported == {"swcraft_tools.reader.read_compositions", "swcraft_tools.schema.Composition"}


def test_an_entry_with_neither_selector_is_a_broken_invariant_and_not_an_answer() -> None:
    """An entry with neither selector reaching the resolver is an assertion, never a silent ``instance-unknown``.

    The loader's exactly-one rule refuses such an entry, so only one built in memory past the schema reaches the
    resolver."""
    identities = leaf.identities_from([_root(_type("solo", "Solo"))])
    with pytest.raises(AssertionError, match="exactly one of swc and instance"):
        identities.resolve(None, None)

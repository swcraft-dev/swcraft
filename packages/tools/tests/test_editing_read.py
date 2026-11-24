"""``read``: the addressed content, and what a vector exchange cannot state about itself.

The three shipped read vectors are the acceptance floor (``test_editing_vectors.py``). Here:

* **a read is not a write channel and discloses no storage**: on the seeded project no layer answer, in any form,
  holds a :data:`STORAGE_MARKERS` entry or the project's location, nor a miss's detail a marker (another model's
  authored value, such as a stub's ``external_ref``, may hold a separator); a read writes no byte and no audit record;
* **vocabulary fidelity as a rule**: every element of every layer is answered in exactly the field
  set the registry publishes for its kind, aliases resolved;
* **the ordering rule**, including the case that separates it from the readers' identity sort and
  the case that shows a file name takes no part in it;
* **the typed miss**, at each of the five places an address can fail;
* **the form**: the default table over the four address shapes, an explicit override of each, the
  echoed form (the one served, not the one asked for), and ``full`` matching the recorded per-layer
  digests, re-recorded for a model change and never for a width change.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import scale_model
import yaml
from conftest import REPO_ROOT, copy_agent_task_bench, write_model_element
from pydantic import BaseModel
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import (
    Address,
    AddressError,
    FieldStep,
    KeyStep,
    MemberStep,
    attribute_for,
    member_identity_of,
    member_matches,
    resolve,
)
from swcraft_tools.editing.discovery import discover
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.protocol import (
    EnvelopeError,
    JoinCondition,
    JoinedRead,
    JoinEntry,
    ReadRequest,
    WhereClause,
)
from swcraft_tools.editing.read import (
    HINT_PAGE_SIZE,
    READ_FORMS,
    RESPONSE_BUDGET_WIRE_BYTES,
    OverBudget,
    ReadMissError,
    UnknownFilterField,
    default_form,
    filter_kinds,
    measure,
    measure_response,
    read,
    read_answer,
    read_value,
)
from swcraft_tools.model_registry import use_registry
from swcraft_tools.project import require_project
from swcraft_tools.query import row_cursor_refusal
from swcraft_tools.snapshot import load_snapshot
from synthetic_support import merged_registry

#: Substrings that would mean a response disclosed where an element is stored: either separator,
#: the file suffix every layer stores, and the two manifest root keys a passport-shaped leak carries.
STORAGE_MARKERS = ("/", "\\", ".yaml", "model_root", "dist_root")

#: The widths, read off the protocol: a form declared there and not exercised here would be a hole.
FORMS = READ_FORMS

#: ``examples/basic``'s model root, the project the byte-identity baselines were taken on.
BASIC_MODEL_ROOT = REPO_ROOT / "examples" / "basic" / "model"

#: Per layer, the SHA-256 of ``json.dumps(read_value(snapshot, Address(layer=L)), ensure_ascii=False)``
#: over ``examples/basic``. The oracle that no width mechanism moves the full form. A change to the
#: MODEL (a declared field published at its default) moves a row: the
#: row is re-recorded, after checking the old digest returns with the new keys removed, and the
#: assertion is never widened.
BASIC_FULL_LAYER_DIGESTS: dict[str, str] = {
    "swc": "2f1a3c3da4780fa4cb9f993278a49c1d6cc61ce4964fd3e3e22f370509e3224f",
    "interfaces": "3bafa53d7fdf391f3566441feba791d199de2b19beb347bd6ef2cefafe58a9f2",
    "connectors": "6b4eec6e55da687ed7a237da1b06f7a1891f696cab0ff1c261602e3f62df1105",
    # The `base_types` answer followed by the `implementation_types` one (`LAYERS_THIS_ROW_SPANS`).
    "implementation_types": "b5e3434c7244e0b2b125581704571bf2346a22422dd8a4ad58bf0e342cdaecde",
    "requirements": "2cd375f3231d98a0545ea8d1961f7a4ab79b5230e2107ae52dc4c0aec2dfdd1b",
    "compositions": "95a53c1d9c588a13ab95b1377cb7f76e5ca556775117e21b50bf9d77b27d0426",
    "tasks": "28deb46ef45353a4a03512b2560465330ef9acf210eea8aeb77a9b58bc0b0e28",
    "sequences": "101e686884082f5c022ca44996cf9d743215bbed9817d56d64b5182bcdcd0515",
    "hardware": "6911b07afe06a26dd920b6ec0e0b02a678e34d5522c48f8359edd97b4ddece3f",
}


def _project(tmp_path: Path, vector: str = "read-swc-element") -> Any:
    root = fixtures.build_fixture(tmp_path / vector, vector)
    return require_project(str(root / "swcraft.yaml"))


#: The reference type :func:`_seed_a_later_kind` adds: its identity sorts FIRST in the readers'
#: ordinal order (``A`` < ``V`` < ``f``) and its kind ranks LAST, the disagreement ordering tests need.
LATER_KIND = "AaaRef"


def _seed_a_later_kind(project: Any) -> None:
    """Give the seeded ``implementation_types`` layer a second kind, ranked after the two value types.

    The ``read-data-types-layer`` vector's layer is one kind, on which the kind rank is a no-op; a
    reference type over one of the value types restores the case.
    """
    write_model_element(
        Path(project.resolved_model_root),
        {"name": LATER_KIND, "kind": "reference_type", "base": "float32"},
    )


@pytest.fixture
def project(tmp_path: Path) -> Any:
    return _project(tmp_path)


@pytest.fixture
def snapshot(project: Any) -> Any:
    return load_snapshot(str(project.resolved_model_root))


def _strings(value: Any) -> list[str]:
    """Every string in a response, keys and values, at every depth.

    Walked rather than ``json.dumps``-ed: the serializer escapes non-ASCII as ``\\uXXXX``, a backslash
    the response does not contain.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [key for key in value] + [item for value_ in value.values() for item in _strings(value_)]
    if isinstance(value, list):
        return [item for value_ in value for item in _strings(value_)]
    return []


def _tree_digest(root: Path) -> str:
    """Every file under *root*, by relative path and content."""
    digest = hashlib.sha256()
    for path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda path: path.as_posix()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


# The response envelope


def test_the_response_carries_exactly_the_four_declared_keys(project: Any) -> None:
    response = read(project, Address(layer="swc", element="ExampleSWC"))
    assert set(response) == {"response", "address", "form", "value"}
    assert response["response"] == "read"


def test_a_read_carries_no_baseline_token_and_no_audit_ref(project: Any) -> None:
    # A read proposes nothing: no token to make conditional, no lifecycle record to point at.
    response = read(project, Address(layer="implementation_types"))
    assert "baseline" not in response
    assert "audit_ref" not in response


def test_the_address_is_echoed_exactly_as_sent(project: Any) -> None:
    sent = {
        "layer": "swc",
        "element": "ExampleSWC",
        "path": [{"field": "ports"}, {"member": "pOut"}, {"field": "interface"}],
    }
    assert read(project, Address.from_json(sent))["address"] == sent


# Granularity: layer, element, field, member, map entry


def test_a_layer_address_answers_every_element(snapshot: Any) -> None:
    value = read_value(snapshot, Address(layer="hardware"))
    # The hardware kinds share one identity namespace and directory, answered in the snapshot's order.
    assert [element["name"] for element in value] == ["CPU", "CORE0", "RAM0", "PARTITION0"]


def test_an_element_address_answers_the_whole_element(snapshot: Any) -> None:
    value = read_value(snapshot, Address(layer="swc", element="ExampleSWC"))
    assert value["name"] == "ExampleSWC"
    assert value["memory"] == {"RAM0": 2048}


def test_a_field_address_answers_the_field(snapshot: Any) -> None:
    address = Address(layer="tasks", element="Task_10ms", path=(FieldStep("wcet"),))
    assert read_value(snapshot, address) == "1200us"


def test_a_member_address_answers_the_member(snapshot: Any) -> None:
    address = Address(layer="swc", element="ExampleSWC", path=(FieldStep("ports"), MemberStep("pOut")))
    assert read_value(snapshot, address) == {
        "name": "pOut",
        "direction": "provide",
        "interface": "IExample",
        "observed_interface": None,
        "description": "Example output port providing data",
        "measures": {},
        "com_specs": [],
        "satisfies": [],
    }


def test_a_map_entry_address_answers_the_entry(snapshot: Any) -> None:
    address = Address(layer="swc", element="ExampleSWC", path=(FieldStep("memory"), KeyStep("RAM0")))
    assert read_value(snapshot, address) == 2048


def test_a_whole_value_member_answers_itself(snapshot: Any) -> None:
    # `Runnable.events` is a `whole_value` list: the member IS its identity, less every field at its
    # contract default, while the value answered is the full one.
    event = {"type": "timing", "period": 0.01}
    address = Address(
        layer="swc",
        element="ExampleSWC",
        path=(FieldStep("runnables"), MemberStep("ExampleRun"), FieldStep("events"), MemberStep(event)),
    )
    assert read_value(snapshot, address) == {**event, "disabled_modes": [], "name": None}


# The vocabulary: exactly what discovery declared, aliases already resolved


def _declared_kind(layer: Any, published: dict[str, Any]) -> Any:
    """The element kind a published element is, read off its discriminator."""
    kinds = layer.top_level_kinds
    if len(kinds) == 1:
        return kinds[0]
    discriminator = kinds[0].discriminator_field
    return next(kind for kind in kinds if published.get(discriminator) == kind.kind)


@pytest.mark.parametrize("layer_name", reg.REGISTRY.layer_names)
def test_every_element_is_answered_in_the_registrys_field_vocabulary(snapshot: Any, layer_name: str) -> None:
    layer = reg.REGISTRY.layer(layer_name)
    # `full` by name: a layer address defaults to `summary`.
    published_elements = read_value(snapshot, Address(layer=layer_name), form="full")
    assert len(published_elements) == len(getattr(snapshot, layer.snapshot_attribute))
    for published in published_elements:
        kind = _declared_kind(layer, published)
        # `all_fields`, not `schema_fields`: a read publishes the placement field too.
        assert tuple(published) == reg.REGISTRY.all_fields(kind.kind)


def test_an_aliased_field_is_answered_under_its_published_name(snapshot: Any) -> None:
    # `Connector.from_` is the one aliased field: addressed, answered and reported as `from`.
    value = read_value(snapshot, Address(layer="connectors", element="Example_to_Consumer"))
    assert "from_" not in value
    # An endpoint names a type, and the unset `composition` is answered as `null`: the file carries
    # one key and the answer carries both (the read side of the exactly-one rule).
    assert value["from"] == {"swc": "ExampleSWC", "composition": None, "port": "pOut"}


def test_a_nested_member_is_answered_in_its_own_kinds_vocabulary(snapshot: Any) -> None:
    address = Address(layer="swc", element="ExampleSWC", path=(FieldStep("runnables"), MemberStep("ExampleRun")))
    assert tuple(read_value(snapshot, address)) == reg.REGISTRY.all_fields("runnable")


# `group`: a declared field answered from placement metadata


def test_group_is_answered_as_a_declared_field(snapshot: Any) -> None:
    value = read_value(snapshot, Address(layer="swc", element="ExampleSWC"))
    assert value["group"] == "example"
    assert list(value)[: len(reg.REGISTRY.all_fields("swc"))] == list(reg.REGISTRY.all_fields("swc"))


def test_group_is_addressable_on_its_own(snapshot: Any) -> None:
    address = Address(layer="swc", element="ExampleSWC", path=(FieldStep("group"),))
    assert read_value(snapshot, address) == "example"


def test_group_reports_the_directory_the_element_actually_sits_in(tmp_path: Path) -> None:
    # Read from placement metadata: moving the file moves the answer; `SWComponent` has no `group`.
    project = _project(tmp_path)
    model_root = Path(project.resolved_model_root)
    moved = model_root / "swc" / "sensors"
    moved.mkdir()
    (model_root / "swc" / "example" / "ExampleSWC.yaml").rename(moved / "ExampleSWC.yaml")

    value = read_value(load_snapshot(str(model_root)), Address(layer="swc", element="ExampleSWC"))
    assert value["group"] == "sensors"


def test_a_layer_read_answers_group_for_every_component(snapshot: Any) -> None:
    assert {element["group"] for element in read_value(snapshot, Address(layer="swc"))} == {"example"}


def test_a_long_but_legal_group_directory_is_still_readable(tmp_path: Path) -> None:
    """A guard on stored data refuses only what the filesystem could not hold.

    The identity bound's 65-byte file-name overhead (``.yaml`` plus the staging suffix) is one a directory never
    carries; a directory inside the component limit and outside the identity bound is read, never a refused layer read.
    """
    project = _project(tmp_path)
    model_root = Path(project.resolved_model_root)
    group = "g" * 200
    assert len(group.encode("utf-8")) <= 255
    moved = model_root / "swc" / group
    moved.mkdir()
    (model_root / "swc" / "example" / "ExampleSWC.yaml").rename(moved / "ExampleSWC.yaml")
    snapshot = load_snapshot(str(model_root))

    # Both read shapes: the whole-layer read is refused entirely by one such file.
    assert read_value(snapshot, Address(layer="swc", element="ExampleSWC"))["group"] == group
    assert group in {element["group"] for element in read_value(snapshot, Address(layer="swc"))}


# Ordering: deterministic and registry-owned


def test_a_layer_answer_is_ordered_by_declared_kind_then_by_the_readers_order(tmp_path: Path) -> None:
    # The loader sorts a layer by identity, ordinally (`AaaRef` < `VehicleSpeed_kph` < `float32`);
    # ranking by declared kind answers the two value types first and the reference type after them.
    project = _project(tmp_path, "read-data-types-layer")
    _seed_a_later_kind(project)
    value = read(project, Address(layer="implementation_types"), form="full")["value"]
    assert [(element["name"], element["kind"]) for element in value] == [
        ("VehicleSpeed_kph", "value_type"),
        ("float32", "value_type"),
        (LATER_KIND, "reference_type"),
    ]


def test_the_same_model_answers_the_same_order_every_time(snapshot: Any) -> None:
    answers = [read_value(snapshot, Address(layer="implementation_types")) for _ in range(3)]
    assert answers[0] == answers[1] == answers[2]


def test_a_file_name_takes_no_part_in_any_forms_order_or_its_pages(tmp_path: Path) -> None:
    """The reader's order inside a kind is identity, whatever the files are called.

    The two value types' files are renamed into the reverse of their identity order; every form and a
    one-element page walk still answer ``VehicleSpeed_kph`` before ``float32``.
    """
    project = _project(tmp_path, "read-data-types-layer")
    _seed_a_later_kind(project)
    directory = Path(project.resolved_model_root) / "data_types" / "implementation"
    (directory / "float32.yaml").rename(directory / "aa.yaml")
    (directory / "VehicleSpeed_kph.yaml").rename(directory / "zz.yaml")
    expected = ["VehicleSpeed_kph", "float32", LATER_KIND]

    for form in ("summary", "full"):
        assert [
            element["name"] for element in read(project, Address(layer="implementation_types"), form=form)["value"]
        ] == (expected), form
    assert read(project, Address(layer="implementation_types"), form="index")["value"]["identities"] == expected

    walked: list[Any] = []
    request: dict[str, Any] = {"form": "index", "limit": 1}
    while True:
        answer = _paged(project, {"layer": "implementation_types"}, **request)
        walked.extend(answer["value"]["identities"])
        if "next" not in answer["page"]:
            break
        request = {"form": "index", "limit": 1, "after": answer["page"]["next"]}
    assert walked == expected


def test_a_single_kind_layer_keeps_the_readers_own_order(snapshot: Any) -> None:
    # A layer with one top-level kind is answered exactly as the reader produced it.
    assert [element["name"] for element in read_value(snapshot, Address(layer="swc"))] == [
        component.name for component in snapshot.swcs
    ]


# The typed miss


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        (Address(layer="no_such_layer"), "no_such_layer"),
        (Address(layer="swc", element="NoSuchSWC"), "no_such_element"),
        (Address(layer="swc", element="ExampleSWC", path=(FieldStep("safety_level"),)), "no_such_field"),
        (
            Address(layer="swc", element="ExampleSWC", path=(FieldStep("ports"), MemberStep("pNope"))),
            "no_such_member",
        ),
        (
            Address(layer="swc", element="ExampleSWC", path=(FieldStep("memory"), KeyStep("ROM"))),
            "no_such_key",
        ),
    ],
    ids=["layer", "element", "field", "member", "key"],
)
def test_an_address_that_resolves_against_nothing_is_a_typed_miss(snapshot: Any, address: Address, reason: str) -> None:
    with pytest.raises(ReadMissError) as raised:
        read_value(snapshot, address)
    assert raised.value.miss.reason == reason
    assert raised.value.miss.address == address


def test_the_miss_names_the_step_that_failed(snapshot: Any) -> None:
    address = Address(layer="swc", element="ExampleSWC", path=(FieldStep("ports"), MemberStep("pNope")))
    with pytest.raises(ReadMissError) as raised:
        read_value(snapshot, address)
    assert raised.value.miss.step_index == 1
    assert raised.value.miss.step == MemberStep("pNope")


def test_a_miss_discloses_no_storage(snapshot: Any) -> None:
    with pytest.raises(ReadMissError) as raised:
        read_value(snapshot, Address(layer="swc", element="NoSuchSWC"))
    assert not any(marker in raised.value.miss.detail for marker in STORAGE_MARKERS)


# A read discloses no storage and changes nothing


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("layer_name", reg.REGISTRY.layer_names)
def test_no_layer_answer_discloses_a_path_extension_or_separator(project: Any, layer_name: str, form: str) -> None:
    # Every form: `group` is in the summary and the full answer, and `index` publishes identities.
    answer = read(project, Address(layer=layer_name), form=form)
    offenders = [text for text in _strings(answer) if any(m in text for m in STORAGE_MARKERS)]
    assert not offenders, f"{layer_name} answered {form} with {offenders} in it"


def test_no_answer_contains_the_projects_own_location(project: Any) -> None:
    # The fixture root is a tmp path no model string can hold, so any component of it is a leak.
    root = Path(project.resolved_model_root)
    serialized = json.dumps(
        [read(project, Address(layer=name), form=form) for name in reg.REGISTRY.layer_names for form in FORMS]
    )
    assert str(root) not in serialized
    assert root.name not in serialized


def test_a_read_leaves_the_project_byte_identical(project: Any) -> None:
    root = Path(project.resolved_project_root)
    before = _tree_digest(root)
    for name in reg.REGISTRY.layer_names:
        read(project, Address(layer=name))
    read(project, Address(layer="swc", element="ExampleSWC"))
    assert _tree_digest(root) == before


def test_a_read_writes_no_audit_record(project: Any) -> None:
    read(project, Address(layer="swc", element="ExampleSWC"))
    assert not (Path(project.resolved_dist_root) / "audit.jsonl").exists()


# The engine seam: unscoped, and refusing what an address cannot express


REQUEST = {"operation": "read", "address": {"layer": "swc", "element": "ExampleSWC"}}


@pytest.mark.parametrize("approved_scope", [None, [], [{"operation": "update", "address": {"layer": "tasks"}}]])
def test_a_read_needs_no_scope_and_no_scope_changes_its_answer(tmp_path: Path, approved_scope: Any) -> None:
    project = _project(tmp_path)
    unscoped = handle_request(REQUEST, project=project, approved_scope=None)
    assert handle_request(REQUEST, project=project, approved_scope=approved_scope) == unscoped


def test_the_engine_dispatches_a_read_to_this_module(tmp_path: Path) -> None:
    project = _project(tmp_path)
    assert handle_request(REQUEST, project=project) == read(project, Address.from_json(REQUEST["address"]))


@pytest.mark.parametrize(
    "address",
    ["model/swc/example/ExampleSWC.yaml", {"layer": "swc", "element": "../../etc/passwd"}],
    ids=["storage-path", "traversal-identity"],
)
def test_the_seam_refuses_what_an_address_cannot_express(tmp_path: Path, address: Any) -> None:
    # Pass A answers both shapes with `schema-violation` before this module runs.
    project = _project(tmp_path)
    answer = handle_request({"operation": "read", "address": address}, project=project)
    assert answer["response"] == "error"
    assert answer["error"] == "schema-violation"
    assert answer["failure_class"] == "invalid"
    # The address model refuses it on its own too.
    with pytest.raises(AddressError):
        Address.from_json(address)


# The form: the default table, the override, and the echo


def _resolution(snapshot: Any, address: Address) -> Any:
    return resolve(address, snapshot, registry=reg.current(), placement={"group": "example"})


@pytest.mark.parametrize(
    ("address", "expected", "shape"),
    [
        (Address(layer="swc"), "summary", "a layer"),
        (
            Address(layer="swc", element="ExampleSWC", path=(FieldStep("ports"),)),
            "summary",
            "a collection field",
        ),
        (Address(layer="swc", element="ExampleSWC"), "full", "an element"),
        (Address(layer="swc", element="ExampleSWC", path=(FieldStep("type"),)), "full", "a scalar field"),
        (
            Address(layer="swc", element="ExampleSWC", path=(FieldStep("ports"), MemberStep("pOut"))),
            "full",
            "a collection member",
        ),
        (
            Address(layer="swc", element="ExampleSWC", path=(FieldStep("memory"), KeyStep("RAM0"))),
            "full",
            "a map entry",
        ),
    ],
    ids=["layer", "collection", "element", "scalar", "member", "entry"],
)
def test_the_default_form_is_the_one_the_address_shape_declares(
    snapshot: Any, address: Address, expected: str, shape: str
) -> None:
    # On the pure function and on the answer, so a served default cannot drift from the table.
    assert default_form(address, _resolution(snapshot, address)) == expected, shape
    assert read_answer(snapshot, address).form == expected, shape


@pytest.mark.parametrize("form", FORMS)
def test_an_explicit_form_overrides_the_default_at_a_layer_address(snapshot: Any, form: str) -> None:
    assert read_answer(snapshot, Address(layer="swc"), form=form).form == form


def test_an_explicit_summary_narrows_an_element_the_default_answers_in_full(snapshot: Any) -> None:
    address = Address(layer="swc", element="ExampleSWC")
    full = read_answer(snapshot, address)
    summary = read_answer(snapshot, address, form="summary")

    assert full.form == "full"
    assert summary.form == "summary"
    assert set(summary.value) < set(full.value)
    # The width went and the depth stayed: kept keys carry the full values, the collection its count.
    assert summary.value["name"] == full.value["name"]
    assert summary.value["group"] == full.value["group"]
    assert summary.value["ports"] == len(full.value["ports"])


def test_an_explicit_full_widens_a_collection_the_default_summarizes(snapshot: Any) -> None:
    address = Address(layer="swc", element="ExampleSWC", path=(FieldStep("ports"),))
    assert read_answer(snapshot, address).form == "summary"
    assert read_answer(snapshot, address, form="full").value == [
        {
            "name": "pOut",
            "direction": "provide",
            "interface": "IExample",
            "observed_interface": None,
            "description": "Example output port providing data",
            "measures": {},
            "com_specs": [],
            "satisfies": [],
        }
    ]


@pytest.mark.parametrize(
    ("address", "shape"),
    [
        (Address(layer="swc", element="ExampleSWC"), "an element"),
        (Address(layer="swc", element="ExampleSWC", path=(FieldStep("type"),)), "a scalar field"),
        (Address(layer="swc", element="ExampleSWC", path=(FieldStep("memory"), KeyStep("RAM0"))), "a map entry"),
    ],
    ids=["element", "scalar", "entry"],
)
def test_index_at_an_address_that_indexes_nothing_is_answered_and_echoed_as_full(
    snapshot: Any, address: Address, shape: str
) -> None:
    # Where there is no collection to name, the answer is the full one and the echo says `full`
    # rather than claiming a narrowing that did not happen.
    indexed = read_answer(snapshot, address, form="index")
    assert indexed.form == "full", shape
    assert indexed.value == read_answer(snapshot, address, form="full").value, shape


def test_a_summary_of_a_scalar_is_the_scalar_and_is_echoed_as_full(snapshot: Any) -> None:
    address = Address(layer="swc", element="ExampleSWC", path=(FieldStep("type"),))
    assert read_answer(snapshot, address, form="summary") == read_answer(snapshot, address, form="full")


def test_the_response_echoes_the_form_it_answered_in(project: Any) -> None:
    assert read(project, Address(layer="swc"))["form"] == "summary"
    assert read(project, Address(layer="swc"), form="full")["form"] == "full"
    assert read(project, Address(layer="swc"), form="index")["form"] == "index"


def test_a_collection_index_is_served_and_echoed_through_the_whole_read_path(project: Any) -> None:
    """``index`` at a COLLECTION, end to end: the ``{count, identities}`` value and echo a client sees.

    The step a hub component's ``ports`` read rests on once the summary is too wide.
    """
    address = Address(layer="swc", element="ExampleSWC", path=(FieldStep("ports"),))
    response = read(project, address, form="index")

    assert response["form"] == "index"
    assert response["address"] == address.to_json()
    assert response["value"] == {"count": 1, "identities": ["pOut"]}


@pytest.mark.parametrize("layer_name", reg.REGISTRY.layer_names)
def test_the_three_forms_answer_one_layer_in_one_order(snapshot: Any, layer_name: str) -> None:
    # The hint chain is walkable only if the layer does not reshuffle under a client.
    layer = reg.REGISTRY.layer(layer_name)
    identity = layer.identity_field
    full = [element[identity] for element in read_value(snapshot, Address(layer=layer_name), form="full")]
    summary = [element[identity] for element in read_value(snapshot, Address(layer=layer_name), form="summary")]
    index = read_value(snapshot, Address(layer=layer_name), form="index")

    assert summary == full
    assert index["identities"] == full
    assert index["count"] == len(full)


# The clause the width mechanism must not break


#: The recorded row spans two layers, compared in the order the kind rank put them, base types first.
LAYERS_THIS_ROW_SPANS: dict[str, tuple[str, ...]] = {"implementation_types": ("base_types", "implementation_types")}


@pytest.mark.parametrize("layer_name", sorted(BASIC_FULL_LAYER_DIGESTS))
def test_the_full_form_is_byte_identical_to_the_recorded_answer(layer_name: str) -> None:
    """``examples/basic`` against recorded digests, re-recorded for a model change, never a width change.

    A recorded digest, not a re-derivation, which would pass however the full form moved.
    """
    snapshot = load_snapshot(str(BASIC_MODEL_ROOT))
    value = [
        element
        for spanned in LAYERS_THIS_ROW_SPANS.get(layer_name, (layer_name,))
        for element in read_value(snapshot, Address(layer=spanned), form="full")
    ]
    serialized = json.dumps(value, ensure_ascii=False).encode("utf-8")
    assert hashlib.sha256(serialized).hexdigest() == BASIC_FULL_LAYER_DIGESTS[layer_name]


def test_a_layer_summary_is_a_fraction_of_its_full_answer_on_a_wide_model(
    wide_model: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The width reduction, on the fixture wide enough for it to matter.

    A ratio, not a byte count, so an unrelated new field does not fail this test. The budget is
    raised out of the way: the fixture is wide because its full read does not fit
    (``test_editing_budget.py``'s subject), and measuring a width needs the answer to exist.
    """
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 100_000_000)
    snapshot = load_snapshot(str(wide_model))
    sizes = {
        form: len(json.dumps(read_value(snapshot, Address(layer="swc"), form=form), ensure_ascii=False).encode("utf-8"))
        for form in FORMS
    }
    assert sizes["summary"] * 10 < sizes["full"]
    assert sizes["index"] < sizes["summary"]


# The form at the engine seam


@pytest.mark.parametrize("form", FORMS)
def test_the_engine_threads_the_requested_form_through(tmp_path: Path, form: str) -> None:
    project = _project(tmp_path)
    request = {"operation": "read", "address": {"layer": "swc"}, "form": form}
    answer = handle_request(request, project=project)
    assert answer["form"] == form
    assert answer == read(project, Address(layer="swc"), form=form)


@pytest.mark.parametrize("form", ["summarised", "FULL", "", 7, None, ["summary"]], ids=repr)
def test_an_unknown_form_is_refused_before_the_model_is_read(tmp_path: Path, form: Any) -> None:
    # Including `None`: `$defs/readForm` is a closed enum without `null`; "the default" omits the key.
    project = _project(tmp_path)
    answer = handle_request({"operation": "read", "address": {"layer": "swc"}, "form": form}, project=project)
    assert answer["response"] == "error"
    assert answer["error"] == "schema-violation"
    assert answer["failure_class"] == "invalid"


# One misplaced file on the default path


def _misplaced_project(tmp_path: Path) -> Any:
    """A project whose SWC layer holds one component two directories deep (``group: "a/b"``, unpublishable)."""
    project = _project(tmp_path)
    swc_dir = Path(project.resolved_model_root) / "swc"
    misplaced = next(swc_dir.rglob("ExampleConsumerSWC.yaml"))
    moved = swc_dir / "a" / "b" / misplaced.name
    moved.parent.mkdir(parents=True, exist_ok=True)
    misplaced.rename(moved)
    return project


def test_one_misplaced_file_refuses_the_layers_default_answer_as_it_refuses_its_full_one(tmp_path: Path) -> None:
    """One misplaced file refuses the layer's default answer as it refuses its full one.

    The summary shares ``_layer_rows`` with the full read: same code, address and message, rather
    than dropping the element and answering a count the model does not have.
    """
    project = _misplaced_project(tmp_path)
    address = {"layer": "swc"}
    default = handle_request({"operation": "read", "address": address}, project=project)
    assert default["response"] == "error"
    assert (default["error"], default["failure_class"]) == ("schema-violation", "invalid")
    assert default["addresses"] == [{"layer": "swc", "element": "ExampleConsumerSWC", "path": [{"field": "group"}]}]

    full = handle_request({"operation": "read", "address": address, "form": "full"}, project=project)
    summary = handle_request({"operation": "read", "address": address, "form": "summary"}, project=project)
    assert default == full == summary


def test_the_index_of_a_layer_with_a_misplaced_file_still_answers(tmp_path: Path) -> None:
    """``index`` carries no ``group``: a misplaced file does not refuse it, so the chain's last step stays walkable."""
    answer = handle_request(
        {"operation": "read", "address": {"layer": "swc"}, "form": "index"},
        project=_misplaced_project(tmp_path),
    )
    assert answer["response"] == "read"
    assert answer["value"] == {"count": 2, "identities": ["ExampleConsumerSWC", "ExampleSWC"]}


# An identity out of an index IS an address step, over every declared collection


@pytest.fixture(scope="module")
def basic_snapshot() -> Any:
    """``examples/basic``, the widest shipped model in declared collections."""
    return load_snapshot(str(BASIC_MODEL_ROOT))


def _round_trip_collections(snapshot: Any, registry: reg.Registry) -> set[str]:
    """Replay every identity of every collection this model reaches, as its own address.

    Depth-first from every element: each identity is wrapped in its step (``key`` for a map entry,
    ``member`` for a list member) and read; nested collections and required-object fields are
    reached at the address a client would walk. Returns the ``kind.field`` names exercised, so the
    caller can state what the fixture did not reach.
    """
    reached: set[str] = set()

    def walk(model: Any, kind: reg.ElementKindDescriptor, address: Address) -> None:
        for field in kind.fields:
            attribute = attribute_for(type(model), field.name)
            if attribute is None:
                continue
            value = getattr(model, attribute, None)
            here = replace(address, path=(*address.path, FieldStep(field.name)))
            if field.collection is None:
                if field.element_kind is not None and isinstance(value, BaseModel):
                    walk(value, registry.element_kind(field.element_kind), here)
                continue
            is_map = isinstance(value, Mapping)
            if value:
                reached.add(f"{kind.kind}.{field.name}")
                served = read_value(snapshot, here, form="index")
                assert served["count"] == len(value), here
                for identity in served["identities"]:
                    step: KeyStep | MemberStep = KeyStep(identity) if is_map else MemberStep(identity)
                    member = replace(here, path=(*here.path, step))
                    # The address resolves and a list member's answer carries the identity back
                    # under `member_matches`; a map entry repeats its key nowhere, so the read suffices.
                    answer = read_value(snapshot, member, form="full")
                    assert is_map or member_matches(answer, field.collection, identity), member
            for item in (value.values() if is_map else value) or ():
                if not isinstance(item, BaseModel):
                    continue
                member_kind = registry.kind_of_model(type(item))
                if member_kind is None:  # pragma: no cover - every shipped member kind is declared
                    continue
                nested = replace(here, path=(*here.path, MemberStep(member_identity_of(item, field.collection))))
                walk(item, member_kind, nested)

    for layer in registry.layers:
        for element in snapshot.state[layer.name]:
            walk(
                element,
                registry.kind_of_model(type(element)),
                Address(layer=layer.name, element=getattr(element, layer.identity_field)),
            )
    return reached


def test_an_identity_out_of_an_index_addresses_the_member_it_names(basic_snapshot: Any) -> None:
    """The index's identities are address steps.

    A composite identity is published as an object, as ``member_matches`` and ``$defs/pathStep`` require; a JSON
    array would replay as ``broken-reference``.
    ``address.member_identity_of`` is the inverse of ``member_matches``; this asserts that over every
    declared collection.

    Does not reach: the collections ``examples/basic`` declares no owner for (asserted below as the
    exact remainder); the wire (``test_editing_budget.py``); whether an index fits a budget.
    """
    registry = reg.current()
    reached = _round_trip_collections(basic_snapshot, registry)
    declared = {
        f"{kind.kind}.{field.name}"
        for kind in registry.kinds.values()
        for field in kind.fields
        if field.collection is not None
    }

    assert reached, "the walk must reach something, or the oracle passes by visiting nothing"
    assert declared - reached == {
        "enum.literals",
        "record.fields",
        "runnable.reads",
        "runnable.writes",
        "runnable.calls",
        "composition.ports",
        "composition.delegations",
        "port.measures",
        "port.com_specs",
        "timing_event.disabled_modes",
        # `examples/basic` declares no data-received event.
        "data_event.disabled_modes",
        "mapping_set.maps",
        "swc.data_type_mappings",
        "implementation_record.elements",
        "texttable.entries",
        "external_interface.operations",
        "external_interface.data_elements",
        # Only the two components carry a link.
        "port.satisfies",
        "runnable.satisfies",
        "composition.satisfies",
        "sender_receiver.satisfies",
        "client_server.satisfies",
        "external_interface.satisfies",
        "task.satisfies",
        "processor.satisfies",
        "core.satisfies",
        "memory.satisfies",
        # `examples/basic` declares no inter-runnable variable
        "swc.variables",
        "runnable.reads_variables",
        "runnable.writes_variables",
        # nor an exclusive area
        "swc.exclusive_areas",
        "runnable.can_enter",
        "runnable.runs_inside",
        "partition.satisfies",
        "requirement.derived_from",
    }, (
        "`examples/basic` declares no `enum`, no `record` and no implementation record (contract "
        "`7.8.0`), none of its runnables declares an "
        "access, its root composition declares members but neither a boundary port "
        "nor a delegation (a flat project has nothing to delegate to), none of "
        "its ports declares a coexistence measure (a project that rates nothing has "
        "no finding for one to answer) or a com-spec entry, none of its timing events disables a "
        "mode (contract `7.11.0` — both record what the model states beyond the silent value, and "
        "this model states nothing), and it declares no mapping set at all (contract `7.0.0` — "
        "its interfaces are typed by implementation types, so its `data_types/mapping_sets/` directory is "
        "present and empty, which is legal, and no component names a set), and no compu method (contract "
        "`8.0.0`'s elements -- its `data_types/compu_methods/` directory is present and empty, which is legal), "
        "and no external interface stub (contract `8.0.0`'s stub lists), and its one trace link sits on each "
        "component itself -- no port, runnable, composition, interface, task or hardware element satisfies a "
        "requirement, and the requirement derives from none (contract `11.0.0`; `swc.satisfies` is walked). "
        "`composition.members` left this remainder with that migration and is walked now; the "
        "remaining `field`-strategy collections are exercised by the siblings the walk does reach, "
        "the `composite` ones by `task.runnables` and `sequence.steps`, and the `whole_value` MAP "
        "by `swc.memory`, which the walk does reach"
    )

    # Independent of the fixture's elements: every (strategy, container) pair the registry declares
    # was walked.
    def combinations(names: set[str]) -> set[tuple[str, str]]:
        return {
            (field.collection.member_identity, field.collection.kind)
            for kind in registry.kinds.values()
            for field in kind.fields
            if field.collection is not None and f"{kind.kind}.{field.name}" in names
        }

    assert combinations(reached) == combinations(declared)


# The filter: `where`
#
# Most cases address `hardware`, the one `examples/basic` layer whose elements are of different
# kinds: `processor` declares neither `threshold` nor `size`, `core` declares `threshold` and an
# optional `hw_id` that `CORE0` leaves empty, `memory` both plus `type`. So "not declared" and
# "declared and empty" (`exists` versus `equals: null`) are both present at one address.


def _filtered(project: Any, address: dict[str, Any], where: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """One filtered read through the engine seam, as a client sends it."""
    request = {"operation": "read", "address": address, "where": where} | extra
    return handle_request(request, project=project)


def _names(answer: dict[str, Any]) -> list[Any]:
    return [element["name"] for element in answer["value"]]


def test_an_equality_clause_selects_the_elements_carrying_that_value(project: Any) -> None:
    # The discriminator, which is what a client filters a mixed layer by.
    assert _names(_filtered(project, {"layer": "hardware"}, [{"field": "kind", "equals": "core"}])) == ["CORE0"]


def test_an_equality_clause_reads_a_scalar_of_any_declared_type(project: Any) -> None:
    # A number compared canonically: `0.8` is `RAM0.threshold`'s alone.
    assert _names(_filtered(project, {"layer": "hardware"}, [{"field": "threshold", "equals": 0.8}])) == ["RAM0"]


def test_an_equality_clause_reads_a_placement_field(snapshot: Any) -> None:
    """``group`` is filterable although no element carries it (placement metadata, ``placement_of``)."""
    matched = read_value(snapshot, Address(layer="swc"), where=(WhereClause(field="group", equals="example"),))
    assert [element["name"] for element in matched] == ["ExampleConsumerSWC", "ExampleSWC"]
    assert read_value(snapshot, Address(layer="swc"), where=(WhereClause(field="group", equals="other"),)) == []


def test_an_exists_clause_selects_both_ways(project: Any) -> None:
    assert _names(_filtered(project, {"layer": "hardware"}, [{"field": "threshold", "exists": True}])) == [
        "CORE0",
        "RAM0",
    ]
    assert _names(_filtered(project, {"layer": "hardware"}, [{"field": "threshold", "exists": False}])) == [
        "CPU",
        "PARTITION0",
    ]


def test_a_field_not_declared_by_a_kind_and_one_declared_but_empty_are_told_apart(project: Any) -> None:
    """``hw_id`` is ``core``'s alone: every element answers ``exists: false``, only ``CORE0`` ``equals: null``."""
    assert _names(_filtered(project, {"layer": "hardware"}, [{"field": "hw_id", "exists": False}])) == [
        "CPU",
        "CORE0",
        "RAM0",
        "PARTITION0",
    ]
    assert _names(_filtered(project, {"layer": "hardware"}, [{"field": "hw_id", "equals": None}])) == ["CORE0"]


def test_clauses_are_and_composed(project: Any) -> None:
    both = [{"field": "threshold", "exists": True}, {"field": "type", "equals": "RAM"}]
    assert _names(_filtered(project, {"layer": "hardware"}, both)) == ["RAM0"]
    # Neither clause alone selects that set, so the conjunction is doing the work.
    assert _names(_filtered(project, {"layer": "hardware"}, both[:1])) == ["CORE0", "RAM0"]


def test_the_empty_result_is_an_answer_and_not_a_refusal(project: Any) -> None:
    """An empty result is a fact about the model; ``unknown-field`` says the question was wrong."""
    answer = _filtered(project, {"layer": "hardware"}, [{"field": "kind", "equals": "gpu"}])
    assert answer["response"] == "read"
    assert answer["value"] == []
    assert answer["form"] == "summary"


def test_a_collection_address_filters_its_members(project: Any) -> None:
    address = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}]}
    provided = _filtered(project, address, [{"field": "direction", "equals": "provide"}])
    assert [member["name"] for member in provided["value"]] == ["pOut"]
    assert _filtered(project, address, [{"field": "direction", "equals": "require"}])["value"] == []


def test_the_response_echoes_the_filter_only_when_there_was_one(project: Any) -> None:
    where = [{"field": "kind", "equals": "core"}]
    assert _filtered(project, {"layer": "hardware"}, where)["where"] == where
    assert "where" not in handle_request({"operation": "read", "address": {"layer": "hardware"}}, project=project)


# The refusals


def test_an_undeclared_field_is_unknown_field(project: Any) -> None:
    answer = _filtered(project, {"layer": "swc"}, [{"field": "safety_level", "exists": True}])
    assert answer["error"] == "unknown-field"
    assert answer["failure_class"] == "invalid"
    assert "safety_level" in answer["message"]
    # The address the client sent: a clause is not a path step.
    assert answer["addresses"] == [{"layer": "swc"}]


def test_an_undeclared_field_is_refused_before_any_element_is_tested(project: Any) -> None:
    # The third clause is the bad one: a request is refused whole, never after a partial pass.
    answer = _filtered(
        project,
        {"layer": "hardware"},
        [
            {"field": "kind", "equals": "core"},
            {"field": "threshold", "exists": True},
            {"field": "voltage", "equals": 5},
        ],
    )
    assert answer["error"] == "unknown-field"
    assert "voltage" in answer["message"]


def test_a_clause_on_a_collection_field_is_schema_violation_naming_its_own_address(project: Any) -> None:
    answer = _filtered(project, {"layer": "swc"}, [{"field": "ports", "equals": 1}])
    assert answer["error"] == "schema-violation"
    assert "'field': 'ports'" in answer["message"], "the message says where to ask the question instead"
    # A hint never changes the address, so descending into the collection is the client's move.
    assert "hints" not in answer


@pytest.mark.parametrize(
    ("address", "shape"),
    [
        ({"layer": "swc", "element": "ExampleSWC"}, "element"),
        ({"layer": "swc", "element": "ExampleSWC", "path": [{"field": "type"}]}, "scalar field"),
        ({"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}, {"member": "pOut"}]}, "member"),
        ({"layer": "swc", "element": "ExampleSWC", "path": [{"field": "memory"}, {"key": "RAM0"}]}, "map entry"),
    ],
    ids=("element", "scalar-field", "member", "map-entry"),
)
def test_a_filter_at_an_address_that_names_one_thing_is_schema_violation(
    project: Any, address: dict[str, Any], shape: str
) -> None:
    """The four shapes with no set to select from refuse a filter, naming the shape; ``where`` is no address step."""
    answer = _filtered(project, address, [{"field": "type", "exists": True}])
    assert answer["error"] == "schema-violation", shape
    assert answer["addresses"] == [address]


def test_a_collection_of_plain_values_declares_no_field_to_filter_by(project: Any) -> None:
    """A ``whole_value`` string list (``memory.accessible``) has no member kind: every clause is ``unknown-field``."""
    address = {"layer": "hardware", "element": "RAM0", "path": [{"field": "accessible"}]}
    answer = _filtered(project, address, [{"field": "name", "exists": True}])
    assert answer["error"] == "unknown-field"
    # No `member_kinds` is no field to filter by; the message is asserted in full below.
    assert "declares no field to filter by" in answer["message"]


def test_a_map_collection_declares_no_field_to_filter_by_either(project: Any) -> None:
    """``swc.memory``, the one shipped map, takes the same answer by its own code path.

    It declares no ``member_kinds``, so every clause is refused before ``select``'s map branch runs:
    this is the assertion the ``pragma`` there cites.
    """
    address = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "memory"}]}
    for clause in ({"field": "RAM0", "exists": True}, {"field": "name", "equals": "RAM0"}):
        answer = _filtered(project, address, [clause])
        assert answer["error"] == "unknown-field", clause
        assert answer["addresses"] == [address]
        assert "declares no field to filter by" in answer["message"], clause


def test_the_filter_refusal_names_the_vocabulary_the_address_actually_has(project: Any) -> None:
    """The advice branches with the noun phrase, because it holds at one shape only.

    At a layer address the filterable kinds are the top-level ones, so the advice points at
    ``member_only``; at a collection address ``filter_kinds`` answers the member kinds, which ARE the
    ``member_only`` ones, so that advice would be false. All four collection-shaped addresses (with
    member kinds, the ``whole_value`` list, the map, the ``members`` slice) take the collection
    message.

    Does not reach whether ``discover`` publishes ``member_kinds`` for the collection (it does not for
    the ``whole_value`` lists or the map, which the message's second half says).
    """
    ports = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}]}
    plain = {"layer": "hardware", "element": "RAM0", "path": [{"field": "accessible"}]}
    mapping = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "memory"}]}
    clause = [{"field": "nope", "exists": True}]

    collections = [_filtered(project, address, clause) for address in (ports, plain, mapping)]
    collections.append(_sliced(project, {"layer": "swc"}, "ports", where=clause))
    for answer in collections:
        assert answer["error"] == "unknown-field"
        assert answer["message"] == (
            "'nope' is not a declared field of any member kind of this collection; the vocabulary here "
            "is the declared fields of the kinds `discover` publishes as this collection's "
            "`member_kinds`, and a collection that publishes none holds plain values and declares no "
            "field to filter by"
        )
        # The layer's advice would be false here: `port` IS `member_only`.
        assert "member_only" not in answer["message"]

    at_layer = _filtered(project, {"layer": "swc"}, clause)
    assert at_layer["error"] == "unknown-field"
    assert at_layer["message"] == (
        "'nope' is not a declared field of any top-level element kind of layer 'swc'; `discover` "
        "publishes the field vocabulary a filter may name, marking the kinds a filter here cannot "
        "reach with `member_only`"
    )


# The filter against the form and the budget


@pytest.mark.parametrize("form", FORMS)
def test_the_filter_runs_before_the_form_in_every_form(project: Any, form: str) -> None:
    """The filter runs before the form: ``index``'s ``count`` is the slice's; after it ``threshold`` is not visible."""
    where = [{"field": "threshold", "exists": True}]
    answered = _filtered(project, {"layer": "hardware"}, where, form=form)
    assert answered["form"] == form
    if form == "index":
        assert answered["value"] == {"count": 2, "identities": ["CORE0", "RAM0"]}
    else:
        assert [element["name"] for element in answered["value"]] == ["CORE0", "RAM0"]


def test_a_filtered_answer_is_measured_as_the_response_that_carries_it(project: Any) -> None:
    """The measure is the real ``readResponse`` envelope, echoed clause included, larger than the unfiltered value."""
    address = Address(layer="hardware")
    where = (WhereClause(field="threshold", exists=True),)
    value = read_value(load_snapshot(str(BASIC_MODEL_ROOT)), address, where=where)
    with_echo = measure_response(value, address, "summary", where=where)
    assert with_echo == measure(
        {
            "response": "read",
            "address": {"layer": "hardware"},
            "form": "summary",
            "where": [{"field": "threshold", "exists": True}],
            "value": value,
        }
    )
    assert with_echo > measure_response(value, address, "summary")


def test_a_slice_answers_where_the_whole_layer_is_refused(wide_model: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One address refused whole and answered as a slice under the same, lowered, budget."""
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 20_000)
    snapshot = load_snapshot(str(wide_model))
    with pytest.raises(OverBudget):
        read_value(snapshot, Address(layer="swc"), form="full")

    sliced = read_value(snapshot, Address(layer="swc"), form="full", where=(WhereClause(field="name", equals="S0"),))
    assert [element["name"] for element in sliced] == ["S0"]


def test_every_hint_of_a_filtered_refusal_replays_the_same_filter(
    wide_model: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hint changes the width alone: dropping the ``where`` would offer the whole layer where a slice did not fit."""
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 800)
    snapshot = load_snapshot(str(wide_model))
    where = (WhereClause(field="group", equals="wide"),)
    with pytest.raises(OverBudget) as refused:
        read_value(snapshot, Address(layer="swc"), form="full", where=where)

    hints = refused.value.hints
    assert hints, "the chain is two steps deep on every layer"
    assert all(hint.where == where for hint in hints)
    assert all(hint.address == Address(layer="swc") for hint in hints)


# The vocabulary is the registry's, not a table here


@pytest.mark.parametrize("layer_name", reg.REGISTRY.layer_names)
def test_every_declared_scalar_field_of_every_layer_is_filterable(snapshot: Any, layer_name: str) -> None:
    """Every non-collection field of every top-level kind is accepted by an ``exists`` clause.

    Asked of the contract, so a new field is covered when declared. Does not reach a field of a kind
    no ``examples/basic`` element has: accepted, selecting nothing.
    """
    layer = reg.REGISTRY.layer(layer_name)
    names = {field.name for kind in layer.top_level_kinds for field in kind.fields if field.collection is None}
    assert names, f"'{layer_name}' declares no scalar field, so this case would assert nothing"
    for name in sorted(names):
        # No refusal is the assertion; what it selects is the model's business.
        read_value(snapshot, Address(layer=layer_name), where=(WhereClause(field=name, exists=True),))


def test_the_filters_vocabulary_is_discovers_kinds_minus_the_member_only_ones(project: Any, snapshot: Any) -> None:
    """``filter_kinds`` answers the kinds ``discover`` publishes without ``member_only``.

    A discovery response carries every element kind, marking file-less ones ``member_only``; a layer
    address filters over the top-level ones. The expected set comes from a real discovery response,
    not from ``layer.top_level_kinds``, the expression under test.

    Does not reach fields: a field only a member-only kind declares (``runnable.events``) is
    published under ``swc`` and refused at the ``swc`` layer address, the limitation
    :func:`~swcraft_tools.editing.read.filter_kinds` records.
    """
    registry = reg.current()
    published = {facts["layer"]: facts["element_kinds"] for facts in discover(project)["layers"]}

    layer = registry.layer("implementation_types")
    resolution = resolve(Address(layer="implementation_types"), snapshot, registry=registry)
    filterable = [kind.kind for kind in filter_kinds(resolution, "layer", layer, registry)]
    assert filterable == [kind["kind"] for kind in published["implementation_types"] if not kind.get("member_only")]
    # The sets differ (`discover` names the member kinds too), so the line above asserts something.
    assert len(published["implementation_types"]) > len(filterable)
    assert [kind.kind for kind in registry.layer("swc").top_level_kinds] == [
        kind["kind"] for kind in published["swc"] if not kind.get("member_only")
    ]

    ports = Address(layer="swc", element="ExampleSWC", path=(FieldStep("ports"),))
    collection = resolve(ports, snapshot, registry=registry)
    assert [kind.kind for kind in filter_kinds(collection, "collection", None, registry)] == ["port"]
    # And a collection's vocabulary is a kind `discover` publishes as member-only.
    assert "port" in {kind["kind"] for kind in published["swc"] if kind.get("member_only")}


# The contract-extension fixture


def test_a_field_declared_only_by_an_extension_contract_is_filterable(tmp_path: Path) -> None:
    """A newly declared field is filterable with no registration.

    The synthetic layer exists in this process alone
    (``packages/tools/tests/fixtures/contracts/model-contract/fixtures/synthetic-layer.json``) and nothing
    under ``packages/tools/src/swcraft_tools/`` names it (``test_synthetic_flow_through.py`` asserts it),
    so its fields are filterable only because :func:`~swcraft_tools.editing.read.filter_kinds` reads the registry.

    Does not reach a field type no shipped kind uses: the filter compares through
    :func:`~swcraft_tools.editing.address.canonical_value`, so a type reducing to no JSON scalar is
    compared as a repr.
    """
    root = fixtures.build_fixture(tmp_path / "eleven", "discover-project-all-layers-present")
    synthetic = Path(root) / "model" / "synthetic"
    synthetic.mkdir(parents=True, exist_ok=True)
    for name, title in (("S1", "One"), ("S2", "Two")):
        (synthetic / f"{name}.yaml").write_text(
            f"name: {name}\ntitle: {title}\ncomposition: VehicleTop\n", encoding="utf-8", newline="\n"
        )

    with use_registry(merged_registry()):
        registry = reg.current()
        eleventh = load_snapshot(str(Path(root) / "model"))
        by_title = read_value(
            eleventh,
            Address(layer="synthetic"),
            form="full",
            where=(WhereClause(field="title", equals="Two"),),
            registry=registry,
        )
        by_reference = read_value(
            eleventh,
            Address(layer="synthetic"),
            form="full",
            where=(WhereClause(field="composition", equals="VehicleTop"),),
            registry=registry,
        )
        with pytest.raises(UnknownFilterField):
            read_value(
                eleventh,
                Address(layer="synthetic"),
                where=(WhereClause(field="colour", exists=True),),
                registry=registry,
            )

    assert [element["name"] for element in by_title] == ["S2"]
    assert [element["name"] for element in by_reference] == ["S1", "S2"]


def test_find_searches_a_layer_with_no_adapter_attribute_as_read_lists_it(tmp_path: Path) -> None:
    """``find`` reads elements by layer name out of ``snapshot.state``, as ``read`` does.

    Guards a search off the adapter attribute, which found nothing in a layer with no
    ``snapshotAttributes`` row while ``read`` listed both elements.
    """
    root = fixtures.build_fixture(tmp_path / "eleven", "discover-project-all-layers-present")
    synthetic = Path(root) / "model" / "synthetic"
    synthetic.mkdir(parents=True, exist_ok=True)
    for name in ("S1", "S2"):
        (synthetic / f"{name}.yaml").write_text(
            f"name: {name}\ntitle: T\ncomposition: VehicleTop\n", encoding="utf-8", newline="\n"
        )

    with use_registry(merged_registry()):
        listed = handle_request({"operation": "read", "address": {"layer": "synthetic"}, "form": "index"}, project=root)
        found = handle_request({"operation": "find", "names": ["S2"]}, project=root)

    assert listed["value"]["identities"] == ["S1", "S2"]
    (entry,) = found["results"]
    assert entry["candidates"][0]["address"] == {"layer": "synthetic", "element": "S2"}
    assert entry["candidates"][0]["tier"] == "exact"


# The page: `limit`/`after`, keyset, in the read order
#
# The partition tests address the data-type layers, whose read order and readers' order DIFFER: the
# seeded form (with the reference type `_seed_a_later_kind` adds) separates the kind rank from the
# identity sort; the shipped twelve-type form separates the ordinal sort from the alphabetical one
# (`int8` follows `int64`).


def _paged(project: Any, address: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """One paged read through the engine seam, as a client sends it."""
    return handle_request({"operation": "read", "address": address} | extra, project=project)


def test_a_page_is_taken_in_the_addresses_own_published_order(tmp_path: Path) -> None:
    """Kind rank, then reader order, never the readers' order alone.

    A page in the readers' collation would answer ``AaaRef`` first, and a client walking pages and one
    reading the whole layer would see two sequences of one set.
    """
    project = _project(tmp_path, "read-data-types-layer")
    _seed_a_later_kind(project)
    whole = _paged(project, {"layer": "implementation_types"}, form="index")["value"]["identities"]
    assert whole == ["VehicleSpeed_kph", "float32", LATER_KIND]

    first = _paged(project, {"layer": "implementation_types"}, form="index", limit=2)
    assert first["value"]["identities"] == ["VehicleSpeed_kph", "float32"]
    assert first["page"] == {"total": 3, "returned": 2, "truncated": True, "next": "float32"}


def test_the_shipped_layers_page_walk_reproduces_its_whole_answer(project: Any) -> None:
    """Twelve data types paged by three concatenate to the layer: none skipped or repeated, ``int8`` after ``int64``."""
    whole = _paged(project, {"layer": "implementation_types"}, form="index")["value"]["identities"]
    assert whole[4:8] == ["int16", "int32", "int64", "int8"]

    walked: list[Any] = []
    request: dict[str, Any] = {"form": "index", "limit": 3}
    while True:
        answer = _paged(project, {"layer": "implementation_types"}, **request)
        walked.extend(answer["value"]["identities"])
        assert answer["page"]["total"] == len(whole)
        if "next" not in answer["page"]:
            break
        request = {"form": "index", "limit": 3, "after": answer["page"]["next"]}
    assert walked == whole


def test_the_page_block_states_total_returned_truncated_and_next(project: Any) -> None:
    """The four figures, and the two places ``truncated`` is not ``next``.

    A whole set asked for as a page is ``truncated: false`` with no ``next``; the last page of a walk
    is ``truncated: true`` with no ``next``.
    """
    # The twelve implementation types.
    whole = _paged(project, {"layer": "implementation_types"}, form="index", limit=30)
    assert whole["page"] == {"total": 12, "returned": 12, "truncated": False}

    last = _paged(project, {"layer": "implementation_types"}, form="index", limit=3, after="uint32")
    assert last["value"]["identities"] == ["uint64", "uint8"]
    assert last["page"] == {"total": 12, "returned": 2, "truncated": True}

    past_the_end = _paged(project, {"layer": "implementation_types"}, form="index", limit=3, after="uint8")
    assert past_the_end["value"] == {"count": 0, "identities": []}
    assert past_the_end["page"] == {"total": 12, "returned": 0, "truncated": True}


def test_a_page_of_one_walks_the_whole_layer(project: Any) -> None:
    """The smallest legal page, which is where an off-by-one shows."""
    identities: list[Any] = []
    cursor: dict[str, Any] = {}
    answer: dict[str, Any] = {}
    for _step in range(12):
        answer = _paged(project, {"layer": "implementation_types"}, form="index", limit=1, **cursor)
        assert answer["page"]["returned"] == 1
        identities.extend(answer["value"]["identities"])
        cursor = {"after": answer["page"]["next"]} if "next" in answer["page"] else {}
    assert identities == _paged(project, {"layer": "implementation_types"}, form="index")["value"]["identities"]
    # The twelfth page ends the walk: `truncated` and no `next`, the client's terminating condition.
    assert answer["page"] == {"total": 12, "returned": 1, "truncated": True}
    assert cursor == {}


def test_the_page_runs_after_the_filter_so_total_counts_the_slice(project: Any) -> None:
    """``total`` is post-filter and pre-page: counting the layer would describe a different question."""
    # On `base_types`: `encoding` is a base type's field.
    answer = _paged(
        project,
        {"layer": "base_types"},
        form="index",
        where=[{"field": "encoding", "equals": "2C"}],
        limit=2,
    )
    assert answer["page"] == {"total": 4, "returned": 2, "truncated": True, "next": "int32_base"}
    assert answer["value"]["identities"] == ["int16_base", "int32_base"]
    assert answer["where"] == [{"field": "encoding", "equals": "2C"}]


@pytest.mark.parametrize("form", FORMS)
def test_the_page_runs_before_the_form_in_every_form(project: Any, form: str) -> None:
    """The page runs before the form: an ``index``'s ``count`` is the window's."""
    answer = _paged(project, {"layer": "implementation_types"}, form=form, limit=2)
    assert answer["form"] == form
    assert answer["page"]["returned"] == 2
    if form == "index":
        assert answer["value"] == {"count": 2, "identities": ["bool", "boolean"]}
    else:
        assert [element["name"] for element in answer["value"]] == ["bool", "boolean"]


def test_a_collection_address_pages_its_members(project: Any) -> None:
    address = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}]}
    answer = _paged(project, address, limit=1)
    assert [member["name"] for member in answer["value"]] == ["pOut"]
    assert answer["page"] == {"total": 1, "returned": 1, "truncated": False}


def test_a_map_collection_pages_by_key_and_answers_a_map(tmp_path: Path) -> None:
    """``swc.memory``'s page is still a map: the key IS the identity, and the cursor."""
    project = _project(tmp_path)
    element = Path(project.resolved_model_root) / "swc" / "example" / "ExampleSWC.yaml"
    element.write_text(
        element.read_text(encoding="utf-8").replace("  RAM0: 2048", "  RAM0: 2048\n  ROM0: 512"),
        encoding="utf-8",
        newline="\n",
    )
    address = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "memory"}]}
    first = _paged(project, address, limit=1)
    assert first["value"] == {"RAM0": 2048}
    assert first["page"] == {"total": 2, "returned": 1, "truncated": True, "next": "RAM0"}

    second = _paged(project, address, limit=1, after="RAM0")
    assert second["value"] == {"ROM0": 512}
    assert second["page"] == {"total": 2, "returned": 1, "truncated": True}


def test_a_composite_identity_is_paged_as_an_object_and_replayed_as_one(tmp_path: Path) -> None:
    """A composite identity is paged as the object a ``member`` step accepts back, so a cursor replays verbatim."""
    project = _project(tmp_path)
    task = Path(project.resolved_model_root) / "tasks" / "Task_10ms.yaml"
    task.write_text(
        task.read_text(encoding="utf-8") + "- swc: ExampleSWC\n  runnable: ExampleRun\n",
        encoding="utf-8",
        newline="\n",
    )
    address = {"layer": "tasks", "element": "Task_10ms", "path": [{"field": "runnables"}]}
    first = _paged(project, address, limit=1)
    cursor = first["page"]["next"]
    # All three keys, the absent selector `null`.
    assert cursor == {"swc": "ExampleConsumerSWC", "instance": None, "runnable": "ExampleConsumerRun"}

    second = _paged(project, address, limit=1, after=cursor)
    assert second["value"] == [{"swc": "ExampleSWC", "runnable": "ExampleRun"}]
    assert second["page"] == {"total": 2, "returned": 1, "truncated": True}


# The page refusals


def test_an_unknown_after_is_a_broken_reference(project: Any) -> None:
    """A cursor the set does not carry is a content miss, never ``unknown-field`` or a silently different window."""
    answer = _paged(project, {"layer": "implementation_types"}, limit=2, after="float128")
    assert answer["error"] == "broken-reference"
    assert answer["failure_class"] == "invalid"
    assert "float128" in answer["message"]
    assert answer["addresses"] == [{"layer": "implementation_types"}]


@pytest.mark.parametrize(
    "address",
    [
        {"layer": "swc", "element": "ExampleSWC"},
        {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "type"}]},
        {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}, {"member": "pOut"}]},
        # The fourth single-thing shape, reached by the filter and member-slice matrices too.
        {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "memory"}, {"key": "RAM0"}]},
    ],
    ids=["element", "scalar-field", "member", "map-entry"],
)
def test_a_page_at_an_address_that_names_one_thing_is_schema_violation(project: Any, address: dict[str, Any]) -> None:
    """A page windows a set; ignoring the ``limit`` at one thing would tell a client it was applied."""
    answer = _paged(project, address, limit=2)
    assert answer["error"] == "schema-violation"
    assert answer["failure_class"] == "invalid"
    assert "`limit`" in answer["message"]
    assert answer["addresses"] == [address]


def test_a_page_refusal_names_the_page_and_not_the_filter(project: Any) -> None:
    """A page refusal and a filter refusal share a code, and the message keeps them apart."""
    paged = _paged(project, {"layer": "swc", "element": "ExampleSWC"}, limit=2)["message"]
    filtered = _filtered(project, {"layer": "swc", "element": "ExampleSWC"}, [{"field": "type", "exists": True}])
    assert "`limit`" in paged and "`where`" not in paged
    assert "`where`" in filtered["message"] and "`limit`" not in filtered["message"]


def test_the_three_not_applicable_messages_name_the_shape_they_were_asked_at(project: Any) -> None:
    """Each refusal names the kind of thing the address named, per shape; swapped nouns would misdirect a client."""
    element = {"layer": "swc", "element": "ExampleSWC"}
    scalar = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "type"}]}
    collection = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}]}

    # `members` is legal at a layer address alone, so all three refuse, each naming a different thing.
    assert "this address names an element" in _sliced_refusal(project, element)
    assert "this address names a single value" in _sliced_refusal(project, scalar)
    assert "this address names one collection already" in _sliced_refusal(project, collection)

    # `where` selects from a set, so the collection answers and the other two refuse with the same nouns.
    where = [{"field": "type", "exists": True}]
    assert "this address names an element" in _filtered(project, element, where)["message"]
    assert "this address names a single value" in _filtered(project, scalar, where)["message"]


def _sliced_refusal(project: Any, address: dict[str, Any]) -> str:
    answer = handle_request(
        {"operation": "read", "address": address, "members": "ports"},
        project=project,
    )
    assert answer["error"] == "schema-violation", answer
    return answer["message"]


def test_after_without_a_limit_is_refused_before_the_model_is_read(project: Any) -> None:
    """``dependentRequired``: a page is a bounded window, refused at pass A before the model opens."""
    answer = _paged(project, {"layer": "implementation_types"}, after="float32")
    assert answer["error"] == "schema-violation"
    with pytest.raises(EnvelopeError, match="bounded window"):
        ReadRequest(address=Address(layer="implementation_types"), after="float32")


@pytest.mark.parametrize(
    "limit",
    [0, -1, 3.5, True, "4", None],
    ids=["zero", "negative", "float", "bool", "string", "null"],
)
def test_a_limit_that_is_not_a_page_size_is_refused(project: Any, limit: Any) -> None:
    """``minimum: 1``, an integer, never an explicit ``null``; ``true`` because ``bool`` is an ``int``."""
    assert _paged(project, {"layer": "implementation_types"}, limit=limit)["error"] == "schema-violation"


# The page against the budget and the chain


def test_the_hint_page_size_is_published_in_the_reads_block(project: Any) -> None:
    """A client reads the offered page size, read at the call, so what is advertised is what is offered."""
    reads = handle_request({"operation": "discover"}, project=project)["reads"]
    assert reads["hint_page_size"] == HINT_PAGE_SIZE
    assert reads["budget_wire_bytes"] == RESPONSE_BUDGET_WIRE_BYTES


def test_the_join_row_ceiling_is_published_in_the_reads_block(project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``reads.join.row_ceiling`` is the ceiling the engine holds, read at the call: lowering one lowers both."""
    import swcraft_tools.editing.read as read_module

    reads = handle_request({"operation": "discover"}, project=project)["reads"]
    assert reads["join"]["row_ceiling"] == read_module.JOIN_ROW_CEILING == 50_000
    monkeypatch.setattr(read_module, "JOIN_ROW_CEILING", 7)
    assert handle_request({"operation": "discover"}, project=project)["reads"]["join"]["row_ceiling"] == 7


def _self_join_request(entries: int) -> dict[str, Any]:
    """A ``swc`` layer read joined *entries* times with itself on ``element``, each entry under its own alias."""
    return {
        "operation": "read",
        "address": {"layer": "swc"},
        "form": "index",
        "join": [
            {"with": {"address": {"layer": "swc"}}, "on": [{"left": "element", "right": "element"}], "as": f"s{n}"}
            for n in range(1, entries + 1)
        ],
    }


def test_a_join_of_max_entries_entries_is_answered_and_one_more_is_refused(project: Any) -> None:
    """``query.JOIN_MAX_ENTRIES`` (8) entries are answered; a ninth is ``schema-violation`` before the model is read."""
    import swcraft_tools.query as query_module

    answered = handle_request(_self_join_request(query_module.JOIN_MAX_ENTRIES), project=project)
    assert answered["response"] == "read", answered
    rows = answered["value"]["identities"]
    assert rows and all(row["s8"]["element"] == row["element"] for row in rows)
    refused = handle_request(_self_join_request(query_module.JOIN_MAX_ENTRIES + 1), project=project)
    assert refused["response"] == "error"
    assert refused["error"] == "schema-violation"
    assert refused["failure_class"] == "invalid"
    assert query_module.join_entries_refusal(9) in refused["message"]


def test_the_join_entry_cap_is_published_in_the_reads_block(project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``reads.join.max_entries`` is ``query.JOIN_MAX_ENTRIES``, read at the call.

    Lowering it on ``query.request`` lowers what is published and what is refused together.
    """
    import swcraft_tools.query.request as query_request

    reads = handle_request({"operation": "discover"}, project=project)["reads"]
    assert reads["join"] == {"row_ceiling": 50_000, "max_entries": query_request.JOIN_MAX_ENTRIES}
    assert query_request.JOIN_MAX_ENTRIES == 8
    monkeypatch.setattr(query_request, "JOIN_MAX_ENTRIES", 2)
    assert handle_request({"operation": "discover"}, project=project)["reads"]["join"]["max_entries"] == 2
    answer = handle_request(_self_join_request(3), project=project)
    assert answer["error"] == "schema-violation"


def test_a_busted_page_is_refused_with_a_hint_at_the_declared_size(
    wide_model: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page that does not fit is refused and offered :data:`~swcraft_tools.editing.read.HINT_PAGE_SIZE`.

    Never a size found by re-measuring (the costliest refusal would project the address again); the
    hint keeps the address, filter and cursor and changes only the window.
    """
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 4_000)
    snapshot = load_snapshot(str(wide_model))
    with pytest.raises(OverBudget) as refused:
        read_value(snapshot, Address(layer="connectors"), form="full", limit=200)

    hints = refused.value.hints
    paged = [hint for hint in hints if hint.limit != 200]
    assert paged, "a served window wider than the declared size is offered the declared size"
    assert [hint.limit for hint in paged] == [HINT_PAGE_SIZE]
    assert paged[-1] is hints[-1], "the page is the chain's last step, below the narrower forms"
    assert [hint.form for hint in hints] == ["summary", "index", "full"]
    assert all(hint.address == Address(layer="connectors") for hint in hints)


def test_a_page_already_at_or_below_the_declared_size_is_not_offered_a_page(
    wide_model: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hint that replays the request is not a step of the chain.

    Judged against what was served, not the requested ``limit``: a window of ten cannot be narrowed to
    fifty (the argument :func:`~swcraft_tools.editing.read.narrower_forms` makes about forms).
    """
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 2_000)
    snapshot = load_snapshot(str(wide_model))
    with pytest.raises(OverBudget) as refused:
        read_value(snapshot, Address(layer="connectors"), form="full", limit=10)
    assert refused.value.hints, "the chain is two steps deep on every layer"
    assert all(hint.limit == 10 for hint in refused.value.hints)


def test_an_unpaged_refusal_of_a_one_page_set_is_still_unchanged_by_the_page_key(
    wide_model: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unpaged refusal of a one-page set is offered no page.

    Twenty elements, so ``min(HINT_PAGE_SIZE, total)`` is the whole set and its page is no step; the
    chain is the narrower-form one, which keeps ``negative/read-layer-full-over-budget.json``
    describing the hints a server sends. A longer set is offered its first page
    (``packages/tools/tests/test_editing_budget.py``).
    """
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 4_000)
    snapshot = load_snapshot(str(wide_model))
    assert scale_model.WIDE_SWCS <= HINT_PAGE_SIZE, "the premise: this layer is one page or less"
    with pytest.raises(OverBudget) as refused:
        read_value(snapshot, Address(layer="swc"), form="full")
    assert [hint.form for hint in refused.value.hints] == ["summary", "index"]
    assert all(hint.limit is None for hint in refused.value.hints)


def test_a_paged_answer_is_measured_as_the_response_that_carries_it() -> None:
    """The measured thing is the real ``readResponse`` envelope, the ``page`` block inside it."""
    address = Address(layer="implementation_types")
    snapshot = load_snapshot(str(BASIC_MODEL_ROOT))
    answer = read_answer(snapshot, address, form="index", limit=3)
    with_page = measure_response(answer.value, address, "index", page=answer.page)
    assert with_page == measure(
        {
            "response": "read",
            "address": {"layer": "implementation_types"},
            "form": "index",
            "page": {"total": 12, "returned": 3, "truncated": True, "next": "float32"},
            "value": answer.value,
        }
    )
    assert with_page > measure_response(answer.value, address, "index")


# The guarantee under an edit, stated and no more


def test_a_keyset_page_perturbs_only_the_edit_point(tmp_path: Path) -> None:
    """A keyset page perturbs only the edit point, and nothing beyond that is claimed.

    A page is re-read from the files, so a walk crossing an edit is not isolated; a cursor anchored to
    a value makes an element removed before it move ``total`` visibly where an offset would skip one.
    """
    project = _project(tmp_path)
    layer = Path(project.resolved_model_root) / "data_types" / "implementation"
    first = _paged(project, {"layer": "implementation_types"}, form="index", limit=4)
    assert first["value"]["identities"] == ["bool", "boolean", "float32", "float64"]
    assert first["page"]["total"] == 12

    # Removed BEFORE the cursor: every later element's offset moves by one, the cursor does not.
    (layer / "bool.yaml").unlink()
    second = _paged(project, {"layer": "implementation_types"}, form="index", limit=4, after=first["page"]["next"])
    assert second["value"]["identities"] == ["int16", "int32", "int64", "int8"]
    assert second["page"]["total"] == 11, "the set moved, and the answer says so"

    # The cursor element itself removed is a refusal, not a different window.
    (layer / "float64.yaml").unlink()
    refused = _paged(project, {"layer": "implementation_types"}, form="index", limit=4, after=first["page"]["next"])
    assert refused["error"] == "broken-reference"


# The member slice: `members`
#
# "Every port on interface A, with its owner" is a question no address can ask (a path without an
# element is not an address), so the slice is a request key, and the other three keys keep their
# meaning over it. The wide fixture (20 components x 50 ports, all on one interface) tests the
# composition; `examples/basic` keeps the shapes readable.


def _sliced(project: Any, address: dict[str, Any], members: str, **extra: Any) -> dict[str, Any]:
    """One member-slice read through the engine seam, as a client sends it."""
    return handle_request({"operation": "read", "address": address, "members": members} | extra, project=project)


def test_every_member_of_a_collection_is_answered_with_the_element_that_owns_it(project: Any) -> None:
    """A row plus the sliced field name is exactly the address of that member."""
    answer = _sliced(project, {"layer": "swc"}, "ports")
    assert answer["form"] == "summary", "a slice answers a set, so it takes the set default"
    assert answer["members"] == "ports"
    assert answer["value"] == [
        {
            "element": "ExampleConsumerSWC",
            "member": {
                "name": "rIn",
                "direction": "require",
                "interface": "IExample",
                "measures": 0,
                "com_specs": 0,
                "satisfies": 0,
            },
        },
        {
            "element": "ExampleSWC",
            "member": {
                "name": "pOut",
                "direction": "provide",
                "interface": "IExample",
                "measures": 0,
                "com_specs": 0,
                "satisfies": 0,
            },
        },
    ]


def test_every_port_on_one_interface_with_its_owners_is_one_request(wide_model: Path) -> None:
    """Every port on one interface with its owner is one request on the wide fixture.

    Through the address vocabulary that is twenty reads, each refused at ``full``, and a client-side join.
    """
    snapshot = load_snapshot(str(wide_model))
    on_the_interface = read_value(
        snapshot,
        Address(layer="swc"),
        form="index",
        members="ports",
        where=(WhereClause(field="interface", equals="ISig"),),
    )
    assert on_the_interface["count"] == 20 * 50
    assert {identity["element"] for identity in on_the_interface["identities"]} == {f"S{i}" for i in range(20)}

    # With a second clause, narrow enough for `summary` under the real budget: one port per component.
    rows = read_value(
        snapshot,
        Address(layer="swc"),
        members="ports",
        where=(WhereClause(field="interface", equals="ISig"), WhereClause(field="name", equals="p0")),
    )
    assert len(rows) == 20
    assert {row["element"] for row in rows} == {f"S{index}" for index in range(20)}
    # `interface` is optional, and a set reference stays in the summary whether required or not.
    assert rows[0] == {
        "element": "S0",
        "member": {
            "name": "p0",
            "direction": "provide",
            "interface": "ISig",
            "measures": 0,
            "com_specs": 0,
            "satisfies": 0,
        },
    }


def test_the_filter_selects_members_and_not_their_owners(project: Any) -> None:
    """``where`` at a slice tests the member: a field only the owning element declares is ``unknown-field``."""
    only_provide = _sliced(project, {"layer": "swc"}, "ports", where=[{"field": "direction", "equals": "provide"}])
    assert [row["element"] for row in only_provide["value"]] == ["ExampleSWC"]
    assert only_provide["where"] == [{"field": "direction", "equals": "provide"}]

    by_owner_field = _sliced(project, {"layer": "swc"}, "ports", where=[{"field": "type", "exists": True}])
    assert by_owner_field["error"] == "unknown-field"
    assert "type" in by_owner_field["message"]
    # The collection's refusal, not the layer's (asserted in full by
    # `test_the_filter_refusal_names_the_vocabulary_the_address_actually_has`).
    assert "member kind of this collection" in by_owner_field["message"]


@pytest.mark.parametrize("form", FORMS)
def test_a_slice_is_published_in_every_form_at_the_collections_own_member_rule(project: Any, form: str) -> None:
    """One set of rows, three widths, at the collection's own member rule: ``index`` is the row minus the member."""
    answer = _sliced(project, {"layer": "swc"}, "ports", form=form)
    assert answer["form"] == form
    if form == "index":
        assert answer["value"] == {
            "count": 2,
            "identities": [
                {"element": "ExampleConsumerSWC", "member": "rIn"},
                {"element": "ExampleSWC", "member": "pOut"},
            ],
        }
    else:
        first = answer["value"][0]["member"]
        assert ("description" in first) == (form == "full")
        assert first["name"] == "rIn"


def test_the_page_windows_the_rows_and_the_cursor_names_one_member_of_one_element(wide_model: Path) -> None:
    """A row identity is the owner and the member: two components may each carry a port ``p0``.

    The cursor is the row minus the member, what the ``index`` form shows.
    """
    snapshot = load_snapshot(str(wide_model))
    first = read_answer(snapshot, Address(layer="swc"), form="index", members="ports", limit=3)
    assert first.value["identities"] == [
        {"element": "S0", "member": "p0"},
        {"element": "S0", "member": "r0"},
        {"element": "S0", "member": "p1"},
    ]
    assert first.page is not None
    assert (first.page.total, first.page.returned, first.page.truncated) == (1000, 3, True)
    assert first.page.next == {"element": "S0", "member": "p1"}

    second = read_answer(snapshot, Address(layer="swc"), form="index", members="ports", limit=2, after=first.page.next)
    assert second.value["identities"] == [{"element": "S0", "member": "r1"}, {"element": "S0", "member": "p2"}]


def test_a_slice_walked_in_pages_reproduces_the_whole_slice(project: Any) -> None:
    """The window property, at the slice: nothing skipped, nothing repeated."""
    whole = _sliced(project, {"layer": "swc"}, "ports", form="index")["value"]["identities"]
    walked: list[Any] = []
    request: dict[str, Any] = {"form": "index", "limit": 1}
    while True:
        answer = _sliced(project, {"layer": "swc"}, "ports", **request)
        walked.extend(answer["value"]["identities"])
        assert answer["page"]["total"] == len(whole)
        if "next" not in answer["page"]:
            break
        request = {"form": "index", "limit": 1, "after": answer["page"]["next"]}
    assert walked == whole


def test_a_filtered_paged_summary_slice_composes_in_one_request(wide_model: Path) -> None:
    """``members``, ``where``, the page and ``form`` in one call, ``page.total`` counting the filtered slice."""
    snapshot = load_snapshot(str(wide_model))
    answer = read_answer(
        snapshot,
        Address(layer="swc"),
        form="summary",
        members="ports",
        where=(WhereClause(field="direction", equals="require"),),
        limit=2,
    )
    assert answer.form == "summary"
    assert answer.page is not None
    assert (answer.page.total, answer.page.returned, answer.page.truncated) == (500, 2, True)
    assert answer.value == [
        {
            "element": "S0",
            "member": {
                "name": "r0",
                "direction": "require",
                "interface": "ISig",
                "measures": 0,
                "com_specs": 0,
                "satisfies": 0,
            },
        },
        {
            "element": "S0",
            "member": {
                "name": "r1",
                "direction": "require",
                "interface": "ISig",
                "measures": 0,
                "com_specs": 0,
                "satisfies": 0,
            },
        },
    ]


def test_a_whole_value_collections_members_are_answered_verbatim_with_their_owners(project: Any) -> None:
    """``memory.accessible``'s plain strings are answered verbatim (``summarize_collection``), each with its memory."""
    answer = _sliced(project, {"layer": "hardware"}, "accessible")
    assert answer["value"] == [{"element": "RAM0", "member": "CORE0"}]
    assert answer["form"] == "summary"
    # Its identity IS the member, so the index carries the same value.
    indexed = _sliced(project, {"layer": "hardware"}, "accessible", form="index")
    assert indexed["value"] == {"count": 1, "identities": [{"element": "RAM0", "member": "CORE0"}]}


def test_a_map_collection_slices_by_key_and_pages_by_it(tmp_path: Path) -> None:
    """``swc.memory``'s row says ``key``, the word the address uses; the cursor is the row minus the value."""
    project = _project(tmp_path)
    element = Path(project.resolved_model_root) / "swc" / "example" / "ExampleSWC.yaml"
    element.write_text(
        element.read_text(encoding="utf-8").replace("  RAM0: 2048", "  RAM0: 2048\n  ROM0: 512"),
        encoding="utf-8",
        newline="\n",
    )
    answer = _sliced(project, {"layer": "swc"}, "memory")
    assert answer["value"] == [
        {"element": "ExampleConsumerSWC", "key": "RAM0", "member": 1536},
        {"element": "ExampleSWC", "key": "RAM0", "member": 2048},
        {"element": "ExampleSWC", "key": "ROM0", "member": 512},
    ]

    paged = _sliced(project, {"layer": "swc"}, "memory", limit=2)
    assert paged["page"] == {
        "total": 3,
        "returned": 2,
        "truncated": True,
        "next": {"element": "ExampleSWC", "key": "RAM0"},
    }
    assert _sliced(project, {"layer": "swc"}, "memory", limit=2, after=paged["page"]["next"])["value"] == [
        {"element": "ExampleSWC", "key": "ROM0", "member": 512}
    ]


def test_the_response_echoes_the_slice_only_when_there_was_one(project: Any) -> None:
    """Nothing in a row names the collection it came from; the echo does."""
    assert _sliced(project, {"layer": "swc"}, "ports")["members"] == "ports"
    assert "members" not in handle_request({"operation": "read", "address": {"layer": "swc"}}, project=project)


def test_a_kind_that_does_not_declare_the_collection_is_a_selection_and_not_an_error(project: Any) -> None:
    """A kind that does not declare the collection is a selection, not an error.

    ``elements`` is declared by ``implementation_record`` alone, which ``examples/basic`` holds none of,
    so the answer is the empty slice.
    """
    answer = _sliced(project, {"layer": "implementation_types"}, "elements")
    assert answer["value"] == []
    assert answer["members"] == "elements"


# The slice refusals


def test_an_undeclared_collection_name_is_unknown_field(project: Any) -> None:
    answer = _sliced(project, {"layer": "swc"}, "connections")
    assert answer["error"] == "unknown-field"
    assert answer["failure_class"] == "invalid"
    assert "connections" in answer["message"]
    # The address is the one the client sent: `members` is not a path step.
    assert answer["addresses"] == [{"layer": "swc"}]


def test_a_declared_field_that_is_not_a_collection_is_schema_violation(project: Any) -> None:
    """``members`` on a scalar is refused, the message pointing at ``where``, the key that answers it."""
    answer = _sliced(project, {"layer": "swc"}, "type")
    assert answer["error"] == "schema-violation"
    assert "`where`" in answer["message"], "the message says which key answers it instead"
    assert answer["addresses"] == [{"layer": "swc"}]


@pytest.mark.parametrize(
    ("address", "shape"),
    [
        ({"layer": "swc", "element": "ExampleSWC"}, "element"),
        ({"layer": "swc", "element": "ExampleSWC", "path": [{"field": "type"}]}, "scalar field"),
        ({"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}]}, "collection"),
        ({"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}, {"member": "pOut"}]}, "member"),
        ({"layer": "swc", "element": "ExampleSWC", "path": [{"field": "memory"}, {"key": "RAM0"}]}, "map entry"),
    ],
    ids=("element", "scalar-field", "collection", "member", "map-entry"),
)
def test_a_slice_at_anything_but_a_layer_address_is_schema_violation(
    project: Any, address: dict[str, Any], shape: str
) -> None:
    """A slice is asked of a layer only; a collection address already answers one element's members."""
    answer = _sliced(project, address, "ports")
    assert answer["error"] == "schema-violation", shape
    assert "`members`" in answer["message"]
    assert answer["addresses"] == [address]


def test_a_slice_refusal_names_the_slice_and_not_the_filter_or_the_page(project: Any) -> None:
    """Slice, filter and page refusals share a code and keep three messages apart."""
    sliced = _sliced(project, {"layer": "swc", "element": "ExampleSWC"}, "ports")["message"]
    assert "`members`" in sliced and "`where`" not in sliced and "`limit`" not in sliced


# The slice against the form, the budget and the chain


def test_a_sliced_answer_is_measured_as_the_response_that_carries_it() -> None:
    """The envelope is the measured thing, and the slice echo is inside it."""
    address = Address(layer="swc")
    snapshot = load_snapshot(str(BASIC_MODEL_ROOT))
    value = read_value(snapshot, address, members="ports")
    with_echo = measure_response(value, address, "summary", members="ports")
    assert with_echo == measure(
        {
            "response": "read",
            "address": {"layer": "swc"},
            "form": "summary",
            "members": "ports",
            "value": value,
        }
    )
    assert with_echo > measure_response(value, address, "summary")


def test_every_hint_of_a_sliced_refusal_replays_the_same_slice(
    wide_model: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hint changes the width and nothing else: dropping ``members`` would ask for the elements."""
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 20_000)
    snapshot = load_snapshot(str(wide_model))
    where = (WhereClause(field="direction", equals="provide"),)
    with pytest.raises(OverBudget) as refused:
        read_value(snapshot, Address(layer="swc"), form="full", members="ports", where=where)

    hints = refused.value.hints
    assert [hint.form for hint in hints] == ["summary", "index", "full"], (
        "the collection's own chain, then the paged step at the form that was served"
    )
    assert [hint.limit for hint in hints] == [None, None, HINT_PAGE_SIZE]
    assert all(hint.members == "ports" for hint in hints)
    assert all(hint.where == where for hint in hints)
    assert all(hint.address == Address(layer="swc") for hint in hints)


def test_a_slice_answers_in_pages_where_no_form_of_it_fits(wide_model: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A thousand ports fit in no width; the same slice with a page does, ``total`` saying what is left."""
    monkeypatch.setattr("swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES", 20_000)
    snapshot = load_snapshot(str(wide_model))
    for form in FORMS:
        with pytest.raises(OverBudget):
            read_value(snapshot, Address(layer="swc"), form=form, members="ports")

    answer = read_answer(snapshot, Address(layer="swc"), form="summary", members="ports", limit=HINT_PAGE_SIZE)
    assert answer.page is not None
    assert (answer.page.total, answer.page.returned) == (1000, HINT_PAGE_SIZE)


def test_a_slice_leaves_the_layers_own_read_untouched(project: Any) -> None:
    """The key is opt-in: a read that names none answers exactly what it did."""
    plain = handle_request({"operation": "read", "address": {"layer": "swc"}}, project=project)
    assert [element["name"] for element in plain["value"]] == ["ExampleConsumerSWC", "ExampleSWC"]
    assert "members" not in plain and "page" not in plain and "where" not in plain


# What the mechanism does not reach, measured


#: The same-named collections two top-level kinds of a layer declare. An external interface stub's
#: member-name lists against the interface kinds' members: ``slice_members`` takes the first
#: declaration and skips the stub's, of another shape. ``satisfies`` on the interface and hardware
#: kinds has one shape, so no element is skipped.
SHARED_COLLECTION_NAMES = {
    ("interfaces", "operations"): ("client_server", "external_interface"),
    ("interfaces", "data_elements"): ("sender_receiver", "external_interface"),
    ("interfaces", "satisfies"): ("sender_receiver", "client_server", "external_interface"),
    ("hardware", "satisfies"): ("processor", "core", "memory", "partition"),
}

#: The pairs above whose declarations share one shape, so the stated limit skips nothing.
SAME_SHAPE_SHARED_NAMES = {("interfaces", "satisfies"), ("hardware", "satisfies")}

#: Every path step past the first at which two kinds declare one collection name; the data-received
#: and timing events' ``disabled_modes`` have one shape, so the slice skips neither.
STEP_SHARED_COLLECTION_NAMES = {
    **SHARED_COLLECTION_NAMES,
    ("swc", "runnables.events.disabled_modes"): ("timing_event", "data_event"),
}


@pytest.mark.parametrize("layer_name", reg.REGISTRY.layer_names)
def test_no_declared_layer_declares_one_collection_name_in_two_top_level_kinds(layer_name: str) -> None:
    """No declared layer declares one collection name in two top-level kinds.

    The stated limit of ``slice_members``: the first declaring kind's collection decides, and an element whose kind
    declares the name with another shape is skipped. Any pair beyond :data:`SHARED_COLLECTION_NAMES` fails here.
    """
    seen: dict[str, list[str]] = {}
    for kind in reg.REGISTRY.layer(layer_name).top_level_kinds:
        for field in kind.fields:
            if field.collection is None:
                continue
            seen.setdefault(field.name, []).append(kind.kind)
    shared = {(layer_name, name): tuple(kinds) for name, kinds in seen.items() if len(kinds) > 1}
    expected = {key: kinds for key, kinds in SHARED_COLLECTION_NAMES.items() if key[0] == layer_name}
    assert shared == expected, (
        f"{sorted(set(shared) ^ set(expected))}: read `slice_members`' stated limit before deciding which "
        "declaration a slice takes"
    )


@pytest.mark.parametrize(("layer_name", "name"), sorted(SAME_SHAPE_SHARED_NAMES))
def test_a_same_shape_shared_name_declares_one_identity_rule(layer_name: str, name: str) -> None:
    """Every kind sharing ``satisfies`` declares the same collection, so none is skipped."""
    declarations = {
        (field.collection.member_identity, field.collection.member_kinds, field.collection.identity_field)
        for kind in reg.REGISTRY.layer(layer_name).top_level_kinds
        for field in kind.fields
        if field.name == name and field.collection is not None
    }
    assert len(declarations) == 1, declarations


def test_a_slice_of_satisfies_answers_every_kind_that_declares_it() -> None:
    """``impact_model`` links one element of each hardware and interface kind, and each answers."""
    snapshot = load_snapshot(
        str(REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "recorded_oracles" / "impact_model")
    )
    hardware = read_answer(snapshot, Address(layer="hardware"), form="full", members="satisfies").value
    interfaces = read_answer(snapshot, Address(layer="interfaces"), form="full", members="satisfies").value
    assert [row["element"] for row in hardware] == ["CPU", "CORE0", "RAM0", "PARTITION0"]
    assert [row["element"] for row in interfaces] == ["ISpeed", "IDiagnostics"]
    assert {row["member"] for row in [*hardware, *interfaces]} == {"REQ_SPEED_001@1"}


def test_a_slice_of_a_shared_collection_name_skips_the_stub_s_list(tmp_path: Path) -> None:
    """An interface stub's listed operation names are no row of the slice."""
    project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
    interfaces = project / "model" / "interfaces"
    (interfaces / "IStubbed.yaml").write_text(
        "name: IStubbed\nkind: external_interface\nexternal_ref: /Ext/IStubbed\n"
        "external_dest: CLIENT-SERVER-INTERFACE\noperations:\n- Get\n",
        encoding="utf-8",
        newline="\n",
    )
    snapshot = load_snapshot(str(project / "model"))
    rows = read_answer(snapshot, Address(layer="interfaces"), form="full", members="operations").value
    assert rows and {row["element"] for row in rows} == {"IDiagnosticService"}, rows
    stub = read_value(snapshot, Address(layer="interfaces", element="IStubbed", path=(FieldStep("operations"),)))
    assert stub == ["Get"]


def test_a_slice_where_only_a_stub_lists_the_name_is_empty_and_no_refusal(tmp_path: Path) -> None:
    """A stub's listed names give an empty slice, neither refusal nor names: they are read at the element's address."""
    project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
    interfaces = project / "model" / "interfaces"
    (interfaces / "IDiagnosticService.yaml").unlink()
    (interfaces / "IStubbed.yaml").write_text(
        "name: IStubbed\nkind: external_interface\nexternal_ref: /Ext/IStubbed\n"
        "external_dest: CLIENT-SERVER-INTERFACE\noperations:\n- Get\n",
        encoding="utf-8",
        newline="\n",
    )
    snapshot = load_snapshot(str(project / "model"))

    assert read_answer(snapshot, Address(layer="interfaces"), form="full", members="operations").value == []


@pytest.mark.parametrize("layer_name", reg.REGISTRY.layer_names)
def test_no_declared_layer_declares_one_name_as_a_collection_and_as_a_scalar(layer_name: str) -> None:
    """The slice's second stated limit: no layer declares one name as a collection and as a scalar.

    A sibling's string would spread one character to a row, a number raise a bare ``TypeError``.
    ``slice_members`` and ``query.slicing._member_rows`` skip what an element's own kind does not
    declare as a collection, so this measures the shipped contract; a new pair would silently change
    which elements a slice answers for.
    """
    collections: dict[str, str] = {}
    scalars: dict[str, str] = {}
    for kind in reg.REGISTRY.layer(layer_name).top_level_kinds:
        for field in kind.fields:
            (collections if field.collection is not None else scalars)[field.name] = kind.kind
    clashing = sorted(set(collections) & set(scalars))
    assert clashing == [], (
        f"in layer '{layer_name}' the name(s) {clashing} are a collection on one top-level kind and a "
        "single value on another; read `slice_members`' stated limits before changing what a slice skips"
    )


# The member path: a dotted `members`
#
# `members: "runnables.writes"` is every member of a collection nested under another, each row
# carrying its owner chain as `through`. The seed is the shipped vector's: the base's
# sender-receiver traffic as runnable accesses.

#: The vector whose seed every test of this section reads.
PATH_SEED = "read-swc-members-path-runnable-writes"


@pytest.fixture
def path_project(tmp_path: Path) -> Any:
    return _project(tmp_path, PATH_SEED)


def yaml_load(path: Path) -> Any:
    """One seeded element file, read for a test to edit."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def yaml_dump(document: Any) -> str:
    return yaml.safe_dump(document, default_flow_style=False, sort_keys=False)


def test_a_path_answers_every_write_of_every_runnable_with_its_owner_chain(path_project: Any) -> None:
    """The row is ``{element, through, member}`` in every form, ``through`` naming the runnable."""
    indexed = _sliced(path_project, {"layer": "swc"}, "runnables.writes", form="index")
    assert indexed["members"] == "runnables.writes"
    assert indexed["value"] == {
        "count": 3,
        "identities": [
            {"element": "ExampleSWC", "through": ["ExampleRun"], "member": {"port": "pOut", "element": "value"}},
            {"element": "ExampleSWC", "through": ["ExampleRun"], "member": {"port": "pPhysical", "element": "speed"}},
            {
                "element": "ExampleSWC",
                "through": ["ExampleSlowRun"],
                "member": {"port": "pPhysical", "element": "speed"},
            },
        ],
    }
    reads = _sliced(path_project, {"layer": "swc"}, "runnables.reads", form="index")["value"]
    assert reads == {
        "count": 2,
        "identities": [
            {
                "element": "ExampleConsumerSWC",
                "through": ["ExampleConsumerRun"],
                "member": {"port": "rIn", "element": "value"},
            },
            {
                "element": "ExampleConsumerSWC",
                "through": ["ExampleConsumerRun"],
                "member": {"port": "rPhysical", "element": "speed"},
            },
        ],
    }


@pytest.mark.parametrize("form", ["summary", "full"])
def test_a_path_carries_through_at_summary_and_full_too(path_project: Any, form: str) -> None:
    """``through`` is how to address the member, so every width publishes it, before the member."""
    rows = _sliced(path_project, {"layer": "swc"}, "runnables.writes", form=form)["value"]
    assert [(row["element"], row["through"]) for row in rows] == [
        ("ExampleSWC", ["ExampleRun"]),
        ("ExampleSWC", ["ExampleRun"]),
        ("ExampleSWC", ["ExampleSlowRun"]),
    ]
    assert all(list(row) == ["element", "through", "member"] for row in rows)
    assert all(row["member"]["port"] and row["member"]["element"] for row in rows)
    assert ("name" in rows[0]["member"]) == (form == "full"), "the unset stated name is a `full`-only field"


def test_the_filter_at_a_path_selects_the_final_member(path_project: Any) -> None:
    """``where`` tests the member at the END of the path, in its own kind's vocabulary."""
    answer = _sliced(
        path_project,
        {"layer": "swc"},
        "runnables.writes",
        form="index",
        where=[{"field": "port", "equals": "pPhysical"}],
    )
    assert [row["through"] for row in answer["value"]["identities"]] == [["ExampleRun"], ["ExampleSlowRun"]]
    # A field only the intermediate member declares is not in that vocabulary.
    refused = _sliced(
        path_project, {"layer": "swc"}, "runnables.writes", where=[{"field": "min_start_interval", "exists": True}]
    )
    assert refused["error"] == "unknown-field"


def test_a_page_continues_past_a_cursor_that_carries_through(path_project: Any) -> None:
    """Two runnables write one ``(port, element)`` pair, so the cursor needs ``through`` to name one row."""
    first = _sliced(path_project, {"layer": "swc"}, "runnables.writes", form="index", limit=2)
    cursor = first["page"]["next"]
    assert cursor == {
        "element": "ExampleSWC",
        "through": ["ExampleRun"],
        "member": {"port": "pPhysical", "element": "speed"},
    }
    second = _sliced(path_project, {"layer": "swc"}, "runnables.writes", form="index", limit=2, after=cursor)
    assert second["value"]["identities"] == [
        {"element": "ExampleSWC", "through": ["ExampleSlowRun"], "member": {"port": "pPhysical", "element": "speed"}}
    ]
    assert "next" not in second["page"]

    # And walked one row at a time it reproduces the whole slice: nothing skipped, nothing repeated.
    whole = _sliced(path_project, {"layer": "swc"}, "runnables.writes", form="index")["value"]["identities"]
    walked: list[Any] = []
    request: dict[str, Any] = {"form": "index", "limit": 1}
    while True:
        answer = _sliced(path_project, {"layer": "swc"}, "runnables.writes", **request)
        walked.extend(answer["value"]["identities"])
        if "next" not in answer["page"]:
            break
        request = {"form": "index", "limit": 1, "after": answer["page"]["next"]}
    assert walked == whole

    # A cursor whose chain names a runnable that writes nothing of the kind is not a row.
    stale = dict(cursor, through=["NoSuchRun"])
    refused = _sliced(path_project, {"layer": "swc"}, "runnables.writes", form="index", limit=1, after=stale)
    assert refused["error"] == "broken-reference"


def test_the_two_events_disabled_modes_declare_one_shape() -> None:
    """The one shared name past a first step is one collection, so neither kind is skipped."""
    declarations = {
        (field.collection.member_identity, field.collection.member_kinds, field.collection.identity_field)
        for kind in (reg.REGISTRY.kinds["timing_event"], reg.REGISTRY.kinds["data_event"])
        for field in kind.fields
        if field.name == "disabled_modes" and field.collection is not None
    }
    assert len(declarations) == 1, declarations


def test_a_three_step_path_reaches_a_collection_only_one_member_kind_declares(tmp_path: Path) -> None:
    """``runnables.events.disabled_modes`` reaches a collection only some event kinds declare.

    The intermediate event is a ``whole_value`` member, so its identity in the chain is the event,
    reduced by its collection's rule.
    """
    project = _project(tmp_path, PATH_SEED)
    component = Path(project.resolved_model_root) / "swc" / "example" / "ExampleSWC.yaml"
    document = yaml_load(component)
    document["runnables"][0]["events"] = [
        {
            "type": "timing",
            "period": 0.01,
            "disabled_modes": [{"port": "mMode", "mode_group_ref": "/Modes/Group", "mode_ref": "/Modes/Group/OFF"}],
        },
        {"type": "init"},
    ]
    document["ports"].append({"name": "mMode", "direction": "require", "interface": "IExample"})
    component.write_text(yaml_dump(document), encoding="utf-8", newline="\n")
    snapshot = load_snapshot(str(project.resolved_model_root))
    rows = read_value(snapshot, Address(layer="swc"), form="index", members="runnables.events.disabled_modes")
    assert rows["count"] == 1
    (row,) = rows["identities"]
    mode = {"port": "mMode", "mode_group_ref": "/Modes/Group", "mode_ref": "/Modes/Group/OFF"}
    # The whole-value event in the chain is reduced: no `name: null`, its declared default.
    assert row == {
        "element": "ExampleSWC",
        "through": ["ExampleRun", {"type": "timing", "period": 0.01, "disabled_modes": [mode]}],
        "member": mode,
    }


def _with_two_events(project: Any) -> None:
    """The provider's first runnable carries a timing event (with ``period``) and an init event (without)."""
    component = Path(project.resolved_model_root) / "swc" / "example" / "ExampleSWC.yaml"
    document = yaml_load(component)
    document["runnables"][0]["events"] = [{"type": "timing", "period": 0.01}, {"type": "init"}]
    component.write_text(yaml_dump(document), encoding="utf-8", newline="\n")


@pytest.mark.parametrize(
    ("clause", "types"),
    [
        ({"field": "period", "equals": 0.01}, ["timing", "timing"]),
        ({"field": "period", "exists": True}, ["timing", "timing", "timing"]),
        ({"field": "period", "exists": False}, ["init"]),
    ],
    ids=("period-equals", "period-exists", "period-absent"),
)
def test_a_filter_at_a_path_ending_at_several_member_kinds_selects(
    path_project: Any, clause: dict[str, Any], types: list[str]
) -> None:
    """``period`` is declared by the timing event and not the init event: a selection, never a refusal."""
    _with_two_events(path_project)
    answer = _sliced(path_project, {"layer": "swc"}, "runnables.events", form="index", where=[clause])
    assert [row["member"]["type"] for row in answer["value"]["identities"]] == types, answer


def test_a_map_ending_path_pages_with_a_cursor_carrying_through_and_key(path_project: Any) -> None:
    """``ports.measures``: rows ``{element, through, key}``, and a cursor naming both continues past one."""
    component = Path(path_project.resolved_model_root) / "swc" / "example" / "ExampleConsumerSWC.yaml"
    document = yaml_load(component)
    document["ports"][0]["measures"] = {"value": "e2e", "other": "crc"}
    component.write_text(yaml_dump(document), encoding="utf-8", newline="\n")
    first = _sliced(path_project, {"layer": "swc"}, "ports.measures", form="index", limit=1)
    cursor = first["page"]["next"]
    assert cursor == {"element": "ExampleConsumerSWC", "through": ["rIn"], "key": "value"}
    second = _sliced(path_project, {"layer": "swc"}, "ports.measures", form="index", limit=1, after=cursor)
    assert second["value"]["identities"] == [{"element": "ExampleConsumerSWC", "through": ["rIn"], "key": "other"}]
    assert "next" not in second["page"]
    full = _sliced(path_project, {"layer": "swc"}, "ports.measures", form="full")["value"]
    assert full == [
        {"element": "ExampleConsumerSWC", "through": ["rIn"], "key": "value", "member": "e2e"},
        {"element": "ExampleConsumerSWC", "through": ["rIn"], "key": "other", "member": "crc"},
    ]


def test_rows_of_one_component_type_are_answered_once_however_often_it_is_placed(path_project: Any) -> None:
    """Rows are per component **type**: a row names the element that declares the members, never a placement."""
    model_root = Path(path_project.resolved_model_root)
    group = model_root / "compositions" / "ConsumerGroup.yaml"
    document = yaml_load(group)
    document["members"].append({"name": "SecondProvider", "type": "ExampleSWC"})
    group.write_text(yaml_dump(document), encoding="utf-8", newline="\n")
    assert _sliced(path_project, {"layer": "swc"}, "runnables.writes", form="index")["value"]["count"] == 3


# The refusals at a path


@pytest.mark.parametrize(
    ("members", "code", "says"),
    [
        ("runnables.writez", "unknown-field", "'writez'"),
        ("ports.nope", "unknown-field", "'nope'"),
        ("runnables.name", "schema-violation", "every step of a member path names a collection"),
        ("memory.anything", "schema-violation", "a map may only be the last step"),
        ("accessible.anything", "unknown-field", "'accessible'"),
    ],
    ids=("undeclared-second-step", "undeclared-under-ports", "scalar-step", "map-mid-path", "undeclared-first-step"),
)
def test_each_path_refusal_has_its_code_and_names_its_step(
    path_project: Any, members: str, code: str, says: str
) -> None:
    answer = _sliced(path_project, {"layer": "swc"}, members)
    assert answer["error"] == code, answer
    assert says in answer["message"]
    assert answer["addresses"] == [{"layer": "swc"}]


def test_a_map_is_refused_mid_path_before_its_next_step_is_resolved(project: Any) -> None:
    """``memory`` is a map, so a step below it is refused by name, whatever that step is."""
    answer = _sliced(project, {"layer": "swc"}, "memory.writes")
    assert answer["error"] == "schema-violation"
    assert "'memory' is a map collection" in answer["message"]


def test_a_path_at_anything_but_a_layer_address_is_schema_violation(path_project: Any) -> None:
    answer = _sliced(path_project, {"layer": "swc", "element": "ExampleSWC"}, "runnables.writes")
    assert answer["error"] == "schema-violation"
    assert "`members`" in answer["message"]


@pytest.mark.parametrize("members", [".", ".ports", "ports.", "runnables..writes"])
def test_an_empty_step_is_refused_by_the_envelope(project: Any, members: str) -> None:
    """An empty step is refused by the envelope as ``schema-violation``."""
    answer = _sliced(project, {"layer": "swc"}, members)
    assert answer["error"] == "schema-violation", answer


@pytest.mark.parametrize("members", ["connections", "foo-bar", "runnables.connections"])
def test_an_undeclared_name_stays_unknown_field(project: Any, members: str) -> None:
    """An undeclared one-step name, a name with no dot that is merely odd, and an undeclared dotted one."""
    answer = _sliced(project, {"layer": "swc"}, members)
    assert answer["error"] == "unknown-field", answer
    assert answer["addresses"] == [{"layer": "swc"}]


# The one-step slice is unchanged


def _one_step_collections(layer: reg.LayerDescriptor) -> list[str]:
    names: list[str] = []
    for kind in layer.top_level_kinds:
        for field in kind.fields:
            if field.collection is not None and field.name not in names:
                names.append(field.name)
    return names


@pytest.mark.parametrize("vector", ["read-swc-element", PATH_SEED])
def test_a_one_step_slice_answers_what_each_elements_collection_address_answers(tmp_path: Path, vector: str) -> None:
    """Byte for byte, every shipped collection of every layer at every form, against an oracle the slice skips.

    The oracle reads each element's own collection address at the same form (never
    :func:`slice_members`) and prefixes each identity or member with its owner; a map entry adds its
    ``key``. No one-step row carries ``through``.
    """
    project = _project(tmp_path, vector)
    snapshot = load_snapshot(str(project.resolved_model_root))
    compared = 0
    for layer in reg.REGISTRY.layers:
        order = read_value(snapshot, Address(layer=layer.name), form="index")["identities"]
        for name in _one_step_collections(layer):
            expected: list[dict[str, Any]] = []
            for element in order:
                try:
                    own = read_value(
                        snapshot, Address(layer=layer.name, element=element, path=(FieldStep(name),)), form="index"
                    )
                except (ReadMissError, AddressError):
                    continue
                is_map = any(
                    kind.get_field(name) is not None and kind.get_field(name).collection.kind == "map"  # type: ignore[union-attr]
                    for kind in layer.top_level_kinds
                )
                expected.extend({"element": element, ("key" if is_map else "member"): i} for i in own["identities"])
            sliced = read_value(snapshot, Address(layer=layer.name), form="index", members=name)
            assert sliced == {"count": len(expected), "identities": expected}, (layer.name, name)
            for form in ("summary", "full"):
                published: list[dict[str, Any]] = []
                for element in order:
                    try:
                        own = read_value(
                            snapshot, Address(layer=layer.name, element=element, path=(FieldStep(name),)), form=form
                        )
                    except (ReadMissError, AddressError):
                        continue
                    if isinstance(own, Mapping):
                        published.extend({"element": element, "key": key, "member": item} for key, item in own.items())
                    else:
                        published.extend({"element": element, "member": item} for item in own)
                rows = read_value(snapshot, Address(layer=layer.name), form=form, members=name)
                assert rows == published, (layer.name, name, form)
                assert all("through" not in row for row in rows), (layer.name, name, form)
            compared += 1
    assert compared > 10


def test_a_legacy_slice_keeps_its_duplicate_identities(tmp_path: Path) -> None:
    """Duplicate identities of a ``whole_value`` multiset (two equal timing events) stay two rows."""
    project = _project(tmp_path, PATH_SEED)
    component = Path(project.resolved_model_root) / "swc" / "example" / "ExampleSWC.yaml"
    document = yaml_load(component)
    document["runnables"][1]["events"] = [{"type": "timing", "period": 0.1}, {"type": "timing", "period": 0.1}]
    component.write_text(yaml_dump(document), encoding="utf-8", newline="\n")
    snapshot = load_snapshot(str(project.resolved_model_root))

    events = read_value(snapshot, Address(layer="swc"), form="index", members="runnables.events")
    slow = [row for row in events["identities"] if row["through"] == ["ExampleSlowRun"]]
    assert len(slow) == 2 and slow[0] == slow[1]
    one_step = read_value(snapshot, Address(layer="swc"), form="summary", members="runnables")
    assert [row["member"]["events"] for row in one_step if row["element"] == "ExampleSWC"] == [1, 2]


# The "only pair" gate, at every step


def _step_kind_sets(layer: reg.LayerDescriptor) -> list[tuple[str, tuple[reg.ElementKindDescriptor, ...]]]:
    """Return every set of kinds a path step resolves against, from the live registry.

    Each collection name follows its first declaration, as :func:`slice_members` does.
    """
    found: list[tuple[str, tuple[reg.ElementKindDescriptor, ...]]] = []
    pending: list[tuple[str, tuple[reg.ElementKindDescriptor, ...]]] = [("", layer.top_level_kinds)]
    while pending:
        prefix, kinds = pending.pop(0)
        found.append((prefix, kinds))
        chosen: dict[str, reg.FieldDescriptor] = {}
        for kind in kinds:
            for field in kind.fields:
                if field.collection is not None and field.name not in chosen:
                    chosen[field.name] = field
        for name, field in chosen.items():
            assert field.collection is not None
            members = tuple(reg.REGISTRY.kinds[k] for k in field.collection.member_kinds if k in reg.REGISTRY.kinds)
            if members and field.collection.kind != "map":
                pending.append((f"{prefix}{name}.", members))
    return found


@pytest.mark.parametrize("layer_name", reg.REGISTRY.layer_names)
def test_no_path_step_declares_one_collection_name_in_two_kinds_but_the_known_pair(layer_name: str) -> None:
    """The first stated limit of ``slice_members``, at every step a path can take, not only the first."""
    shared: dict[tuple[str, str], tuple[str, ...]] = {}
    clashing: list[str] = []
    for prefix, kinds in _step_kind_sets(reg.REGISTRY.layer(layer_name)):
        collections: dict[str, list[str]] = {}
        scalars: set[str] = set()
        for kind in kinds:
            for field in kind.fields:
                if field.collection is not None:
                    collections.setdefault(field.name, []).append(kind.kind)
                else:
                    scalars.add(field.name)
        shared |= {
            (layer_name, f"{prefix}{name}"): tuple(owners) for name, owners in collections.items() if len(owners) > 1
        }
        clashing.extend(f"{prefix}{name}" for name in sorted(set(collections) & scalars))
    expected = {key: kinds for key, kinds in STEP_SHARED_COLLECTION_NAMES.items() if key[0] == layer_name}
    assert shared == expected, (
        f"{sorted(set(shared) ^ set(expected))}: read `slice_members`' stated limit before deciding which "
        "declaration a path step takes"
    )
    assert clashing == [], f"{clashing}: a collection on one kind and a single value on another at one path step"


# Computed values: `derived` and the identity rows
#
# A read carrying `derived` answers identity rows at an explicit `form: "index"`: each identity as
# `{"element": ...}`, its computed values under `"derived"`, identical rows collapsed, the page cursor
# the whole row. The seed is the member path's.


def _rows(project: Any, address: dict[str, Any], derived: list[str], **extra: Any) -> dict[str, Any]:
    """One identity-row read through the engine seam, as a client sends it."""
    return handle_request(
        {"operation": "read", "address": address, "form": "index", "derived": derived} | extra, project=project
    )


#: The seed's two connectors, each resolved through the group's delegation.
SEED_CONNECTION_ROWS = [
    {
        "element": "Example_to_Consumer",
        "derived": {
            "connection_ends": {
                "provider": {"swc": "ExampleSWC", "port": "pOut"},
                "requester": {"swc": "ExampleConsumerSWC", "port": "rIn"},
            }
        },
    },
    {
        "element": "Physical_to_Consumer",
        "derived": {
            "connection_ends": {
                "provider": {"swc": "ExampleSWC", "port": "pPhysical"},
                "requester": {"swc": "ExampleConsumerSWC", "port": "rPhysical"},
            }
        },
    },
]


def test_connection_ends_are_answered_through_the_delegations(path_project: Any) -> None:
    answer = _rows(path_project, {"layer": "connectors"}, ["connection_ends"])
    assert answer["response"] == "read", answer
    assert answer["form"] == "index"
    assert answer["derived"] == ["connection_ends"]
    assert answer["value"] == {"count": 2, "identities": SEED_CONNECTION_ROWS}


def test_base_type_is_answered_on_each_access_through_its_mapping(path_project: Any) -> None:
    """An implementation-typed element and an application-typed one paired by the component's set."""
    answer = _rows(path_project, {"layer": "swc"}, ["base_type"], members="runnables.reads")
    assert answer["response"] == "read", answer
    rows = answer["value"]["identities"]
    assert [row["member"] for row in rows] == [
        {"port": "rIn", "element": "value"},
        {"port": "rPhysical", "element": "speed"},
    ]
    assert {row["derived"]["base_type"]["implementation"] for row in rows} == {"float32"}
    assert all(row["through"] == ["ExampleConsumerRun"] for row in rows)
    assert list(rows[0]) == ["element", "through", "member", "derived"]


def test_effective_level_is_answered_once_per_component(path_project: Any) -> None:
    """The seed allocates no level, so both components are ``QM`` (the vector shows a declared and a derived one)."""
    answer = _rows(path_project, {"layer": "swc"}, ["effective_level"])
    assert answer["response"] == "read", answer
    assert answer["value"]["identities"] == [
        {"element": "ExampleConsumerSWC", "derived": {"effective_level": {"level": "QM", "rating": "QM"}}},
        {"element": "ExampleSWC", "derived": {"effective_level": {"level": "QM", "rating": "QM"}}},
    ]


def test_partitions_answers_each_placed_component_and_no_row_for_the_unplaced(path_project: Any) -> None:
    """The seed runs the consumer in ``PARTITION0`` and the provider in no task: one row (the vector shows two)."""
    answer = _rows(path_project, {"layer": "swc"}, ["partitions"])
    assert answer["response"] == "read", answer
    assert answer["value"]["identities"] == [
        {"element": "ExampleConsumerSWC", "derived": {"partitions": {"partition": "PARTITION0", "core": "CORE0"}}},
    ]


def test_crossing_ends_answers_nothing_where_an_end_runs_in_no_partition(path_project: Any) -> None:
    """The provider runs in no task, so the seed's connectors cross nothing: an empty answer, never a refusal."""
    answer = _rows(path_project, {"layer": "connectors"}, ["crossing_ends"])
    assert answer["response"] == "read", answer
    assert answer["value"] == {"count": 0, "identities": []}


def test_effective_level_and_partitions_are_answered_together(path_project: Any) -> None:
    """The two names share the components: the product of their values, and no row without a partition."""
    answer = _rows(path_project, {"layer": "swc"}, ["effective_level", "partitions"])
    assert answer["response"] == "read", answer
    assert answer["value"]["identities"] == [
        {
            "element": "ExampleConsumerSWC",
            "derived": {
                "effective_level": {"level": "QM", "rating": "QM"},
                "partitions": {"partition": "PARTITION0", "core": "CORE0"},
            },
        },
    ]


def test_a_derived_read_applies_where_before_the_values(path_project: Any) -> None:
    answer = _rows(
        path_project,
        {"layer": "swc"},
        ["base_type"],
        members="runnables.writes",
        where=[{"field": "port", "equals": "pPhysical"}],
    )
    assert answer["where"] == [{"field": "port", "equals": "pPhysical"}]
    assert [row["through"] for row in answer["value"]["identities"]] == [["ExampleRun"], ["ExampleSlowRun"]]


def _fan_out(project: Any) -> None:
    """Delegate the group's `rIn` twice: one connector reaches two requester ends, `connection_ends` lists it twice."""
    root = Path(project.resolved_model_root)
    consumer = yaml_load(root / "swc" / "example" / "ExampleConsumerSWC.yaml")
    second = {**consumer, "name": "SecondConsumerSWC"}
    (root / "swc" / "example" / "SecondConsumerSWC.yaml").write_text(yaml_dump(second), encoding="utf-8")
    group = yaml_load(root / "compositions" / "ConsumerGroup.yaml")
    group["members"].append({"name": "SecondConsumerSWC", "type": "SecondConsumerSWC"})
    group["delegations"].append(
        {"name": "rIn_to_second", "port": "rIn", "member": "SecondConsumerSWC", "member_port": "rIn"}
    )
    (root / "compositions" / "ConsumerGroup.yaml").write_text(yaml_dump(group), encoding="utf-8")


def test_a_set_valued_name_multiplies_rows_and_a_whole_row_cursor_pages_each_once(path_project: Any) -> None:
    _fan_out(path_project)
    whole = _rows(path_project, {"layer": "connectors"}, ["connection_ends"])["value"]["identities"]
    assert len(whole) == 3
    assert [row["element"] for row in whole] == ["Example_to_Consumer", "Example_to_Consumer", "Physical_to_Consumer"]
    walked: list[Any] = []
    after: Any = None
    while True:
        page = _rows(
            path_project,
            {"layer": "connectors"},
            ["connection_ends"],
            limit=1,
            **({} if after is None else {"after": after}),
        )
        assert page["response"] == "read", page
        walked.extend(page["value"]["identities"])
        assert page["page"]["total"] == 3
        if "next" not in page["page"]:
            break
        after = page["page"]["next"]
        assert after == walked[-1], "the cursor is the whole row, derived included"
    assert walked == whole


def test_an_invalid_models_identical_rows_collapse_to_one(path_project: Any) -> None:
    """Two delegations of one boundary port to one inner port reach the same end twice."""
    root = Path(path_project.resolved_model_root)
    group = yaml_load(root / "compositions" / "ConsumerGroup.yaml")
    group["delegations"].append(
        {"name": "rIn_to_consumer_again", "port": "rIn", "member": "ExampleConsumerSWC", "member_port": "rIn"}
    )
    (root / "compositions" / "ConsumerGroup.yaml").write_text(yaml_dump(group), encoding="utf-8")
    answer = _rows(path_project, {"layer": "connectors"}, ["connection_ends"])
    assert answer["value"] == {"count": 2, "identities": SEED_CONNECTION_ROWS}


def test_a_cursor_the_rows_do_not_carry_is_broken_reference(path_project: Any) -> None:
    stale = {**SEED_CONNECTION_ROWS[0], "derived": {"connection_ends": None}}
    answer = _rows(path_project, {"layer": "connectors"}, ["connection_ends"], limit=1, after=stale)
    assert answer["error"] == "broken-reference", answer


@pytest.mark.parametrize(
    ("address", "derived", "extra", "code"),
    [
        ({"layer": "swc"}, ["no_such_value"], {"members": "runnables.writes"}, "unknown-field"),
        ({"layer": "swc"}, ["connection_ends"], {}, "schema-violation"),
        ({"layer": "swc"}, ["base_type"], {}, "schema-violation"),
        ({"layer": "swc"}, ["base_type"], {"members": "ports"}, "schema-violation"),
        ({"layer": "connectors"}, ["base_type"], {}, "schema-violation"),
        ({"layer": "connectors"}, ["effective_level"], {}, "schema-violation"),
        ({"layer": "swc"}, ["effective_level"], {"members": "runnables.reads"}, "schema-violation"),
        ({"layer": "connectors"}, ["partitions"], {}, "schema-violation"),
        ({"layer": "swc"}, ["crossing_ends"], {}, "schema-violation"),
    ],
    ids=[
        "outside-the-vocabulary",
        "ends-at-swc",
        "base-type-at-components",
        "base-type-at-ports",
        "base-type-at-connectors",
        "effective-level-at-connectors",
        "effective-level-at-a-member-path",
        "partitions-at-connectors",
        "crossing-ends-at-components",
    ],
)
def test_a_derived_name_outside_its_at_is_refused_never_blank(
    path_project: Any, address: dict[str, Any], derived: list[str], extra: dict[str, Any], code: str
) -> None:
    answer = _rows(path_project, address, derived, **extra)
    assert answer["response"] == "error", answer
    assert answer["error"] == code
    assert answer["addresses"] == [address]
    assert "`derived`" in answer["message"]


def test_the_members_refusal_comes_before_the_derived_one(path_project: Any) -> None:
    """Members, then where, then derived: a misspelt path is told about first."""
    answer = _rows(path_project, {"layer": "swc"}, ["no_such_value"], members="runnables.writez")
    assert answer["error"] == "unknown-field"
    assert "writez" in answer["message"]


@pytest.mark.parametrize(
    "body",
    [
        {"address": {"layer": "swc", "element": "ExampleSWC"}, "form": "index", "derived": ["base_type"]},
        {"address": {"layer": "connectors"}, "derived": ["connection_ends"]},
        {"address": {"layer": "connectors"}, "form": "summary", "derived": ["connection_ends"]},
    ],
    ids=["element-address", "no-form", "summary-form"],
)
def test_the_envelope_refuses_derived_off_a_layer_or_without_index(path_project: Any, body: dict[str, Any]) -> None:
    answer = handle_request({"operation": "read", **body}, project=path_project)
    assert answer["error"] == "schema-violation", answer


def test_a_derived_read_over_the_budget_is_offered_a_page_that_replays(
    path_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Page hints only, each carrying `form` and `derived`; the hint is a request the server answers."""
    import swcraft_tools.editing.read as read_module

    _fan_out(path_project)
    answered = _rows(path_project, {"layer": "connectors"}, ["connection_ends"])
    budget = measure(answered) - 1
    monkeypatch.setattr(read_module, "RESPONSE_BUDGET_WIRE_BYTES", budget)
    monkeypatch.setattr(read_module, "HINT_PAGE_SIZE", 1)
    refused = _rows(path_project, {"layer": "connectors"}, ["connection_ends"])
    assert refused["error"] == "over-budget", refused
    assert refused["measured"]["elements"] == 3
    hints = refused["hints"]
    assert hints == [
        {
            "operation": "read",
            "address": {"layer": "connectors"},
            "form": "index",
            "limit": 1,
            "derived": ["connection_ends"],
        }
    ]
    replayed = handle_request(hints[0], project=path_project)
    assert replayed["response"] == "read", replayed
    assert replayed["value"]["identities"] == answered["value"]["identities"][:1]


def test_one_row_over_the_budget_is_refused_with_no_hint(path_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Today's eligibility rule is kept: no page is offered at or below `HINT_PAGE_SIZE` rows."""
    import swcraft_tools.editing.read as read_module

    monkeypatch.setattr(read_module, "RESPONSE_BUDGET_WIRE_BYTES", 10)
    refused = _rows(path_project, {"layer": "connectors"}, ["connection_ends"], limit=1)
    assert refused["error"] == "over-budget", refused
    assert "hints" not in refused


def test_the_reads_block_publishes_the_validators_vocabulary(path_project: Any) -> None:
    from swcraft_tools.validator import DERIVED

    reads = discover(path_project)["reads"]
    assert reads["derived"] == [entry.as_json() for entry in DERIVED]
    assert [entry["name"] for entry in reads["derived"]] == [
        "connection_ends",
        "base_type",
        "effective_level",
        "partitions",
        "crossing_ends",
        "unconnected",
        "core",
        "realised_by",
        "allocation",
        "stale_links",
    ]


def test_every_published_at_is_declared_by_the_live_registry() -> None:
    """The ``at`` of each published entry names a declared layer and, where given, a declared collection path."""
    from swcraft_tools.validator import DERIVED

    for entry in DERIVED:
        layer = reg.REGISTRY.layer(entry.at["layer"])
        for path in entry.at.get("members", []):
            kinds = layer.top_level_kinds
            for step in path.split("."):
                fields = [field for field in (kind.get_field(step) for kind in kinds) if field is not None]
                assert fields and fields[0].collection is not None, (entry.name, path, step)
                kinds = tuple(reg.REGISTRY.element_kind(name) for name in fields[0].collection.member_kinds)


# The join: inner, on equal values at statically checked row paths
#
# An outer row is kept once per joined row whose `right` values equal its `left` values, the match
# nested under `as`. Every path is checked against the declared row shape first; the nested reads
# are answered unmeasured, and only the joined answer meets the budget.

#: The sender-receiver connection table request, as its vector ships it.
PILOT_JOIN_REQUEST: dict[str, Any] = fixtures.load_vectors()["read-swc-writes-joined-to-readers"]["input"]

#: One entry: each write joined to the connector whose provider end it is.
WRITES_TO_CONNECTORS: dict[str, Any] = {
    "with": {"address": {"layer": "connectors"}, "derived": ["connection_ends"]},
    "on": [
        {"left": "element", "right": "derived.connection_ends.provider.swc"},
        {"left": "member.port", "right": "derived.connection_ends.provider.port"},
    ],
    "as": "connection",
}


def _joined_read(project: Any, join: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """The writes of every runnable, joined by *join*, through the engine seam."""
    body = {"operation": "read", "address": {"layer": "swc"}, "form": "index", "members": "runnables.writes"}
    return handle_request(body | {"join": join} | extra, project=project)


def _table(answer: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    """The joined rows as (writer runnable, port, connector, reader component, reader runnable)."""
    return [
        (
            row["through"][0],
            row["member"]["port"],
            row["connection"]["element"],
            row["reader"]["element"],
            row["reader"]["through"][0],
        )
        for row in answer["value"]["identities"]
    ]


def test_a_one_entry_join_nests_each_match_under_its_alias(path_project: Any) -> None:
    answer = _joined_read(path_project, [WRITES_TO_CONNECTORS])
    assert answer["response"] == "read", answer
    assert answer["join"] == [WRITES_TO_CONNECTORS]
    rows = answer["value"]["identities"]
    assert [(row["through"], row["member"]["port"], row["connection"]["element"]) for row in rows] == [
        (["ExampleRun"], "pOut", "Example_to_Consumer"),
        (["ExampleRun"], "pPhysical", "Physical_to_Consumer"),
        (["ExampleSlowRun"], "pPhysical", "Physical_to_Consumer"),
    ]
    assert list(rows[0]) == ["element", "through", "member", "connection"]
    assert rows[0]["connection"] == SEED_CONNECTION_ROWS[0]


def test_the_two_entry_pilot_request_answers_the_sender_receiver_table(path_project: Any) -> None:
    answer = handle_request(PILOT_JOIN_REQUEST, project=path_project)
    assert answer["response"] == "read", answer
    assert _table(answer) == [
        ("ExampleRun", "pOut", "Example_to_Consumer", "ExampleConsumerSWC", "ExampleConsumerRun"),
        ("ExampleRun", "pPhysical", "Physical_to_Consumer", "ExampleConsumerSWC", "ExampleConsumerRun"),
        ("ExampleSlowRun", "pPhysical", "Physical_to_Consumer", "ExampleConsumerSWC", "ExampleConsumerRun"),
    ]
    assert {row["derived"]["base_type"]["implementation"] for row in answer["value"]["identities"]} == {"float32"}


def test_a_fan_out_multiplies_rows_and_a_non_match_is_dropped(path_project: Any) -> None:
    _fan_out(path_project)
    answer = handle_request(PILOT_JOIN_REQUEST, project=path_project)
    assert _table(answer) == [
        ("ExampleRun", "pOut", "Example_to_Consumer", "ExampleConsumerSWC", "ExampleConsumerRun"),
        ("ExampleRun", "pOut", "Example_to_Consumer", "SecondConsumerSWC", "ExampleConsumerRun"),
        ("ExampleRun", "pPhysical", "Physical_to_Consumer", "ExampleConsumerSWC", "ExampleConsumerRun"),
        ("ExampleSlowRun", "pPhysical", "Physical_to_Consumer", "ExampleConsumerSWC", "ExampleConsumerRun"),
    ]
    only_physical = {
        **WRITES_TO_CONNECTORS,
        "with": {**WRITES_TO_CONNECTORS["with"], "where": [{"field": "name", "equals": "Physical_to_Consumer"}]},
    }
    narrowed = _joined_read(path_project, [only_physical])
    assert [row["member"]["port"] for row in narrowed["value"]["identities"]] == ["pPhysical", "pPhysical"]


def test_a_page_replay_of_a_joined_answer_yields_every_row_once(path_project: Any) -> None:
    _fan_out(path_project)
    whole = handle_request(PILOT_JOIN_REQUEST, project=path_project)["value"]["identities"]
    walked: list[Any] = []
    after: Any = None
    while True:
        page = handle_request(
            PILOT_JOIN_REQUEST | {"limit": 1} | ({} if after is None else {"after": after}), project=path_project
        )
        assert page["response"] == "read", page
        walked.extend(page["value"]["identities"])
        if "next" not in page["page"]:
            break
        after = page["page"]["next"]
        assert after == walked[-1], "the cursor is the whole row, every alias included"
    assert walked == whole
    assert len(walked) == 4


@pytest.mark.parametrize(
    "address",
    [
        {"layer": "swc", "element": "ExampleSWC"},
        {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "runnables"}]},
        {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}, {"member": "pOut"}]},
    ],
    ids=["element", "field", "member"],
)
def test_join_at_a_non_layer_address_is_refused(path_project: Any, address: dict[str, Any]) -> None:
    body = {"operation": "read", "address": address, "form": "index", "join": [WRITES_TO_CONNECTORS]}
    answer = handle_request(body, project=path_project)
    assert answer["error"] == "schema-violation", answer


def test_a_bad_path_is_refused_on_an_empty_model(path_project: Any) -> None:
    """Decided from the declarations: a model with no component and no connector is refused all the same."""
    root = Path(path_project.resolved_model_root)
    for layer in ("swc", "connectors"):
        for path in (root / layer).rglob("*.yaml"):
            path.unlink()
    good = _joined_read(path_project, [WRITES_TO_CONNECTORS])
    assert good["value"] == {"count": 0, "identities": []}, good
    bad = {**WRITES_TO_CONNECTORS, "on": [{"left": "member", "right": "derived.connection_ends.provider.port"}]}
    answer = _joined_read(path_project, [bad])
    assert answer["error"] == "schema-violation", answer
    assert answer["addresses"] == [{"layer": "swc"}]
    assert "join 'connection': `left` path 'member'" in answer["message"]
    assert "'member.port'" in answer["message"], "the refusal lists the values the row does declare"


@pytest.mark.parametrize(
    ("joined", "code"),
    [
        ({"address": {"layer": "connectors"}, "derived": ["base_type"]}, "schema-violation"),
        ({"address": {"layer": "connectors"}, "derived": ["nope"]}, "unknown-field"),
        ({"address": {"layer": "connectors"}, "members": "nope"}, "unknown-field"),
        ({"address": {"layer": "connectors"}, "where": [{"field": "nope", "equals": 1}]}, "unknown-field"),
        ({"address": {"layer": "no_such_layer"}}, "unknown-field"),
        ({"address": {"layer": "swc"}, "where": [{"field": "nope", "equals": 1}]}, "unknown-field"),
        ({"address": {"layer": "swc"}, "members": "runnables.nope"}, "unknown-field"),
        ({"address": {"layer": "swc"}, "members": "name"}, "schema-violation"),
        ({"address": {"layer": "swc"}, "where": [{"field": "ports", "exists": True}]}, "schema-violation"),
    ],
    ids=[
        "derived-at-another-set",
        "derived-unknown",
        "members-unknown",
        "where-unknown",
        "layer-undeclared",
        "same-layer-where-unknown",
        "same-layer-members-unknown",
        "same-layer-members-not-a-collection",
        "same-layer-where-at-a-collection",
    ],
)
def test_a_joined_reads_own_refusal_carries_its_address_and_names_its_alias(
    path_project: Any, joined: dict[str, Any], code: str
) -> None:
    """A joined read's refusal has the type, code and address the read would earn alone.

    The message names the entry, since the ``swc`` address alone cannot tell which of the two reads it was about.
    """
    entry = {"with": joined, "on": [{"left": "element", "right": "element"}], "as": "connection"}
    answer = _joined_read(path_project, [entry])
    assert answer["error"] == code, answer
    assert answer["addresses"] == [joined["address"]]
    assert answer["message"].startswith("join 'connection': "), answer


def test_a_join_on_a_through_step_tells_two_rows_apart_by_their_runnable(path_project: Any) -> None:
    """``through.0`` is read off the row's owner chain: two writes of one port match only their own runnable's task."""
    root = Path(path_project.resolved_model_root)
    task = yaml_load(root / "tasks" / "Task_10ms.yaml")
    task["runnables"].append({"swc": "ExampleSWC", "runnable": "ExampleSlowRun"})
    (root / "tasks" / "Task_10ms.yaml").write_text(yaml_dump(task), encoding="utf-8")
    entry = {
        "with": {"address": {"layer": "tasks"}, "members": "runnables"},
        "on": [{"left": "element", "right": "member.swc"}, {"left": "through.0", "right": "member.runnable"}],
        "as": "task",
    }
    answer = _joined_read(path_project, [entry])
    assert answer["value"]["identities"] == [
        {
            "element": "ExampleSWC",
            "through": ["ExampleSlowRun"],
            "member": {"port": "pPhysical", "element": "speed"},
            "task": {
                "element": "Task_10ms",
                "member": {"swc": "ExampleSWC", "instance": None, "runnable": "ExampleSlowRun"},
            },
        }
    ], answer


def test_a_join_on_a_map_key_and_on_a_whole_value_member(path_project: Any) -> None:
    """A map-ending path's row carries ``key``, a whole-value member's ``member``; each joins on its own leaf."""

    def self_join(members: str, path: str) -> list[tuple[str, str]]:
        body = {
            "operation": "read",
            "address": {"layer": "swc"},
            "form": "index",
            "members": members,
            "join": [
                {
                    "with": {"address": {"layer": "swc"}, "members": members},
                    "on": [{"left": path, "right": path}],
                    "as": "peer",
                }
            ],
        }
        answer = handle_request(body, project=path_project)
        assert answer["response"] == "read", answer
        return [(row["element"], row["peer"]["element"]) for row in answer["value"]["identities"]]

    root = Path(path_project.resolved_model_root)
    consumer = yaml_load(root / "swc" / "example" / "ExampleConsumerSWC.yaml")
    consumer["memory"] = {"ROM0": 512}
    (root / "swc" / "example" / "ExampleConsumerSWC.yaml").write_text(yaml_dump(consumer), encoding="utf-8")
    assert self_join("memory", "key") == [("ExampleConsumerSWC", "ExampleConsumerSWC"), ("ExampleSWC", "ExampleSWC")]
    assert self_join("data_type_mappings", "member") == [
        ("ExampleConsumerSWC", "ExampleConsumerSWC"),
        ("ExampleConsumerSWC", "ExampleSWC"),
        ("ExampleSWC", "ExampleConsumerSWC"),
        ("ExampleSWC", "ExampleSWC"),
    ]


def test_a_stale_cursor_on_a_joined_answer_is_broken_reference(path_project: Any) -> None:
    first = _joined_read(path_project, [WRITES_TO_CONNECTORS], limit=1)
    stale = {**first["page"]["next"], "connection": {"element": "Removed_Connector"}}
    answer = _joined_read(path_project, [WRITES_TO_CONNECTORS], limit=1, after=stale)
    assert answer["error"] == "broken-reference", answer
    # One sentence for both surfaces: `swcraft query --request` differs only in rendering the cursor.
    assert answer["message"] == row_cursor_refusal(json.dumps(stale, ensure_ascii=False)), answer


def test_a_direct_caller_is_held_to_the_envelopes_rows_rule(path_project: Any) -> None:
    """``read_value`` is an entry point of its own: ``derived`` without ``form: "index"`` is refused."""
    snapshot = load_snapshot(str(path_project.resolved_model_root))
    with pytest.raises(EnvelopeError, match='states `form: "index"`'):
        read_value(snapshot, Address(layer="connectors"), form="full", derived=["connection_ends"])
    with pytest.raises(EnvelopeError, match="a read carrying `derived`"):
        read_value(snapshot, Address(layer="swc", element="ExampleSWC"), form="index", derived=["base_type"])


def _self_join_entries(entries: int) -> tuple[JoinEntry, ...]:
    """*entries* Python-built ``swc`` self-joins on ``element``, each under its own alias."""
    return tuple(
        JoinEntry(
            read=JoinedRead(address=Address(layer="swc")),
            on=(JoinCondition(left="element", right="element"),),
            alias=f"s{n}",
        )
        for n in range(1, entries + 1)
    )


def test_a_python_built_request_is_held_to_the_join_entry_cap() -> None:
    """The cap is the construction path's, not the JSON parser's alone: nine Python-built entries are refused."""
    import swcraft_tools.query as query_module

    admitted = ReadRequest(Address(layer="swc"), form="index", join=_self_join_entries(query_module.JOIN_MAX_ENTRIES))
    assert len(admitted.join) == query_module.JOIN_MAX_ENTRIES
    with pytest.raises(EnvelopeError) as refusal:
        ReadRequest(Address(layer="swc"), form="index", join=_self_join_entries(query_module.JOIN_MAX_ENTRIES + 1))
    assert str(refusal.value) == query_module.join_entries_refusal(9)


def test_a_direct_read_answer_caller_is_held_to_the_join_entry_cap(path_project: Any) -> None:
    """``read_answer`` is an entry point of its own: a ninth join entry is refused with the one sentence."""
    import swcraft_tools.query as query_module

    snapshot = load_snapshot(str(path_project.resolved_model_root))
    with pytest.raises(EnvelopeError) as refusal:
        read_answer(
            snapshot,
            Address(layer="swc"),
            form="index",
            join=_self_join_entries(query_module.JOIN_MAX_ENTRIES + 1),
        )
    assert str(refusal.value) == query_module.join_entries_refusal(9)


def test_prefix_sharing_aliases_both_answer(path_project: Any) -> None:
    second = {**WRITES_TO_CONNECTORS, "as": "connection2"}
    answer = _joined_read(path_project, [WRITES_TO_CONNECTORS, second])
    rows = answer["value"]["identities"]
    assert rows and all(row["connection"] == row["connection2"] for row in rows)


def test_a_join_at_the_ceiling_is_admitted_and_one_row_over_is_refused_with_its_exact_weight(
    path_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling counts rows exactly: at it the answer is unchanged, one under it the entry is refused.

    ``elements`` is the row count and ``wire_bytes`` (from the buckets) the unpaged answer's size.
    """
    import swcraft_tools.editing.read as read_module

    _fan_out(path_project)
    [first, _second] = PILOT_JOIN_REQUEST["join"]
    request = PILOT_JOIN_REQUEST | {"join": [first]}
    answered = handle_request(request, project=path_project)
    assert answered["response"] == "read", answered
    rows = len(answered["value"]["identities"])
    monkeypatch.setattr(read_module, "JOIN_ROW_CEILING", rows)
    assert handle_request(request, project=path_project) == answered, "a product at the ceiling is admitted"
    monkeypatch.setattr(read_module, "JOIN_ROW_CEILING", rows - 1)
    refused = handle_request(request, project=path_project)
    assert refused["error"] == "over-budget", refused
    assert refused["measured"] == {
        "wire_bytes": measure(answered),
        "budget": read_module.RESPONSE_BUDGET_WIRE_BYTES,
        "elements": rows,
    }
    assert "hints" not in refused
    assert f"join row ceiling of {rows - 1}" in refused["message"]


def test_a_join_over_the_ceiling_is_refused_before_it_is_built_with_no_hint(
    path_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused whatever ``limit`` it carries: every page builds the whole join, so a page narrows nothing."""
    import swcraft_tools.editing.read as read_module

    _fan_out(path_project)
    whole = handle_request(PILOT_JOIN_REQUEST, project=path_project)
    rows = whole["value"]["identities"]
    monkeypatch.setattr(read_module, "JOIN_ROW_CEILING", len(rows) - 1)
    monkeypatch.setattr(read_module, "HINT_PAGE_SIZE", 1)
    for request in (
        PILOT_JOIN_REQUEST,
        PILOT_JOIN_REQUEST | {"limit": 1},
        PILOT_JOIN_REQUEST | {"limit": 3, "after": rows[0]},
    ):
        refused = handle_request(request, project=path_project)
        assert refused["error"] == "over-budget", refused
        assert refused["failure_class"] == "invalid"
        assert refused["addresses"] == [{"layer": "swc"}]
        assert refused["measured"]["elements"] > len(rows) - 1
        assert refused["measured"]["budget"] == read_module.RESPONSE_BUDGET_WIRE_BYTES
        assert "hints" not in refused, "no page hint and no `where` hint: neither is a sound step"
        assert "every page builds the whole join" in refused["message"]
        assert "with `where`" in refused["message"]


def test_a_refused_join_narrowed_with_where_answers(path_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `where` on the joined read brings the join under the ceiling, which holds both entries' rows summed."""
    import swcraft_tools.editing.read as read_module

    _fan_out(path_project)
    [first, second] = PILOT_JOIN_REQUEST["join"]
    whole = handle_request(PILOT_JOIN_REQUEST, project=path_project)
    rows = whole["value"]["identities"]
    built = len(handle_request(PILOT_JOIN_REQUEST | {"join": [first]}, project=path_project)["value"]["identities"])
    narrowed_first = {
        **first,
        "with": {**first["with"], "where": [{"field": "name", "equals": "Physical_to_Consumer"}]},
    }
    narrowed_request = PILOT_JOIN_REQUEST | {"join": [narrowed_first, second]}
    narrowed_built = len(
        handle_request(PILOT_JOIN_REQUEST | {"join": [narrowed_first]}, project=path_project)["value"]["identities"]
    ) + len(handle_request(narrowed_request, project=path_project)["value"]["identities"])
    ceiling = built + len(rows) - 1
    assert narrowed_built <= ceiling
    monkeypatch.setattr(read_module, "JOIN_ROW_CEILING", ceiling)
    assert handle_request(PILOT_JOIN_REQUEST, project=path_project)["error"] == "over-budget"
    narrowed = handle_request(narrowed_request, project=path_project)
    assert narrowed["response"] == "read", narrowed
    assert narrowed["value"]["identities"] == [
        row for row in rows if row["connection"]["element"] == "Physical_to_Consumer"
    ]


def test_an_earlier_entry_is_refused_on_the_rows_it_would_build(
    path_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entry by entry: an entry over the ceiling is refused before a later one could drop its rows."""
    import swcraft_tools.editing.read as read_module

    _fan_out(path_project)
    [first, _second] = PILOT_JOIN_REQUEST["join"]
    alone = handle_request(PILOT_JOIN_REQUEST | {"join": [first]}, project=path_project)
    assert alone["response"] == "read", alone
    dropping = {
        "with": {"address": {"layer": "swc"}, "where": [{"field": "name", "equals": "NoSuchSWC"}]},
        "on": [{"left": "element", "right": "element"}],
        "as": "nobody",
    }
    request = PILOT_JOIN_REQUEST | {"join": [first, dropping]}
    assert handle_request(request, project=path_project)["value"] == {"count": 0, "identities": []}
    monkeypatch.setattr(read_module, "JOIN_ROW_CEILING", len(alone["value"]["identities"]) - 1)
    refused = handle_request(request, project=path_project)
    assert refused["error"] == "over-budget", refused
    assert refused["measured"]["elements"] == len(alone["value"]["identities"])
    assert f"by entry '{first['as']}'" in refused["message"]


def test_the_ceiling_bounds_the_whole_join_summed_over_its_entries(
    path_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cumulative, not per entry: every entry under the ceiling, their sum over it, is refused.

    A per-entry ceiling would admit up to ``query.JOIN_MAX_ENTRIES`` entries just under it. At a
    ceiling one under the two entries' sum, the second is refused before it builds a row; ``elements``
    is the sum and ``wire_bytes`` the refused entry's unpaged answer.
    """
    import swcraft_tools.editing.read as read_module

    _fan_out(path_project)
    [first, second] = PILOT_JOIN_REQUEST["join"]
    alone = handle_request(PILOT_JOIN_REQUEST | {"join": [first]}, project=path_project)
    whole = handle_request(PILOT_JOIN_REQUEST, project=path_project)
    assert alone["response"] == whole["response"] == "read", (alone, whole)
    built_first = len(alone["value"]["identities"])
    built_second = len(whole["value"]["identities"])
    total = built_first + built_second
    assert 0 < built_first < total and 0 < built_second < total
    monkeypatch.setattr(read_module, "JOIN_ROW_CEILING", total)
    assert handle_request(PILOT_JOIN_REQUEST, project=path_project) == whole, "the sum exactly at the ceiling"
    monkeypatch.setattr(read_module, "JOIN_ROW_CEILING", total - 1)
    assert handle_request(PILOT_JOIN_REQUEST | {"join": [first]}, project=path_project) == alone
    refused = handle_request(PILOT_JOIN_REQUEST, project=path_project)
    assert refused["error"] == "over-budget", refused
    assert refused["measured"] == {
        "wire_bytes": measure(whole),
        "budget": read_module.RESPONSE_BUDGET_WIRE_BYTES,
        "elements": total,
    }
    assert "hints" not in refused
    assert (
        f"would build {total} row(s) in total by entry '{second['as']}' ({built_second} of them" in (refused["message"])
    )
    assert f"join row ceiling of {total - 1} rows for the whole join" in refused["message"]


def test_a_join_under_the_ceiling_whose_answer_busts_is_offered_a_page_that_replays_and_no_form(
    path_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the ceiling a join is a set like any other: over the budget it is offered the page alone."""
    import swcraft_tools.editing.read as read_module

    answered = handle_request(PILOT_JOIN_REQUEST, project=path_project)
    monkeypatch.setattr(read_module, "RESPONSE_BUDGET_WIRE_BYTES", measure(answered) - 1)
    monkeypatch.setattr(read_module, "HINT_PAGE_SIZE", 1)
    refused = handle_request(PILOT_JOIN_REQUEST, project=path_project)
    assert refused["error"] == "over-budget", refused
    assert refused["measured"]["elements"] == len(answered["value"]["identities"]) == 3
    expected_hint = PILOT_JOIN_REQUEST | {"limit": 1}
    assert refused["hints"] == [expected_hint], "the page alone: no narrower form under `join`"
    replayed = handle_request(refused["hints"][0], project=path_project)
    assert replayed["response"] == "read", replayed
    assert replayed["value"]["identities"] == answered["value"]["identities"][:1]


def test_a_joined_page_over_the_budget_is_offered_a_page_carrying_its_after_verbatim(
    path_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A walk that busts continues from where it stopped: the hint keeps the request's whole-row cursor."""
    import swcraft_tools.editing.read as read_module

    _fan_out(path_project)
    whole = handle_request(PILOT_JOIN_REQUEST, project=path_project)["value"]["identities"]
    request = PILOT_JOIN_REQUEST | {"limit": 3, "after": whole[0]}
    answered = handle_request(request, project=path_project)
    monkeypatch.setattr(read_module, "RESPONSE_BUDGET_WIRE_BYTES", measure(answered) - 1)
    monkeypatch.setattr(read_module, "HINT_PAGE_SIZE", 1)
    refused = handle_request(request, project=path_project)
    assert refused["error"] == "over-budget", refused
    assert refused["hints"] == [request | {"limit": 1}]
    replayed = handle_request(refused["hints"][0], project=path_project)
    assert replayed["value"]["identities"] == [whole[1]]


def test_one_joined_row_over_the_budget_is_refused_with_no_hint(
    path_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import swcraft_tools.editing.read as read_module

    monkeypatch.setattr(read_module, "RESPONSE_BUDGET_WIRE_BYTES", 10)
    refused = handle_request(PILOT_JOIN_REQUEST | {"limit": 1}, project=path_project)
    assert refused["error"] == "over-budget", refused
    assert "hints" not in refused


def test_a_nested_read_over_the_budget_is_not_refused(path_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the joined answer is measured: a nested read the budget would refuse on its own is answered inside it."""
    import swcraft_tools.editing.read as read_module

    root = Path(path_project.resolved_model_root)
    for number in range(60):
        (root / "connectors" / f"Spare_{number:02d}.yaml").write_text(
            yaml_dump(
                {
                    "name": f"Spare_{number:02d}",
                    "from": {"swc": "ExampleSWC", "port": "pOut"},
                    "to": {"swc": "ExampleConsumerSWC", "port": "rIn"},
                }
            ),
            encoding="utf-8",
        )
    body = {
        "operation": "read",
        "address": {"layer": "connectors"},
        "form": "index",
        "where": [{"field": "name", "equals": "Example_to_Consumer"}],
        "join": [
            {
                "with": {"address": {"layer": "connectors"}},
                "on": [{"left": "element", "right": "element"}],
                "as": "same",
            }
        ],
    }
    answered = handle_request(body, project=path_project)
    assert answered["value"] == {
        "count": 1,
        "identities": [{"element": "Example_to_Consumer", "same": {"element": "Example_to_Consumer"}}],
    }
    monkeypatch.setattr(read_module, "RESPONSE_BUDGET_WIRE_BYTES", measure(answered))
    assert handle_request(body, project=path_project) == answered
    nested_alone = handle_request(
        {"operation": "read", "address": {"layer": "connectors"}, "form": "index"}, project=path_project
    )
    assert nested_alone["error"] == "over-budget", "the nested read alone busts, so the case is not vacuous"


# A size refusal names the CLI where the CLI can answer
#
# `swcraft query --request` answers a layer address in `index` (with or without `members`, `derived`
# or `join`) or `full` form, so the sentence is carried by those refusals (paged and join-ceiling;
# `full` in test_read_names_the_cli_at_full.py) and by no `summary`, below-layer or `discover`
# refusal. The hints are unchanged.

#: A layer read the CLI answers, without and with each of the row keys.
_CLI_SHAPED_REQUESTS: dict[str, dict[str, Any]] = {
    "plain index": {"operation": "read", "address": {"layer": "swc"}, "form": "index"},
    "members": {"operation": "read", "address": {"layer": "swc"}, "form": "index", "members": "runnables.writes"},
    "derived and join": PILOT_JOIN_REQUEST,
    "where over derived": {
        "operation": "read",
        "address": {"layer": "swc"},
        "form": "index",
        "derived": ["effective_level"],
        "where": [{"field": "derived.effective_level.rating", "equals": "QM"}],
    },
}


def _cli_answer(body: dict[str, Any], project: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any):
    """*body* minus `operation`, written to a file and answered as JSON by the command the sentence names.

    The argv is read off :data:`CLI_ANSWERS_SENTENCE`, so a sentence naming a command that cannot
    answer (one omitting the required `--project`) fails here.
    """
    import shlex

    from swcraft_tools.cli import main
    from swcraft_tools.editing.read import CLI_ANSWERS_SENTENCE

    request = tmp_path / "cli-request.json"
    request.write_text(
        json.dumps({key: value for key, value in body.items() if key != "operation"}), encoding="utf-8", newline="\n"
    )
    manifest = str(Path(project.resolved_project_root) / "swcraft.yaml")
    written = shlex.split(CLI_ANSWERS_SENTENCE.split("`")[1])
    assert written[:2] == ["swcraft", "query"] and "FILE" in written and "PATH" in written, written
    substitutes = {"FILE": str(request), "PATH": manifest}
    monkeypatch.setattr("sys.argv", [substitutes.get(token, token) for token in written] + ["--format", "json"])
    capsys.readouterr()
    main()
    document = json.loads(capsys.readouterr().out)
    return document["rows"] if "rows" in document else document["elements"]


def _assert_carries_the_sentence_once_at_its_end(refused: dict[str, Any]) -> str:
    """The message with the sentence removed — which is what the refusal said before the sentence existed."""
    from swcraft_tools.editing.read import CLI_ANSWERS_SENTENCE

    assert refused["error"] == "over-budget", refused
    message = refused["message"]
    assert message.endswith(f"; {CLI_ANSWERS_SENTENCE}"), message
    assert message.count(CLI_ANSWERS_SENTENCE) == 1
    return message.removesuffix(f"; {CLI_ANSWERS_SENTENCE}")


@pytest.mark.parametrize("case", sorted(_CLI_SHAPED_REQUESTS))
def test_a_paged_refusal_of_a_layer_index_names_the_cli_and_keeps_its_hints(
    case: str, path_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over the budget, a layer `index` read gains the sentence; the message before it and the hints are unchanged."""
    import swcraft_tools.editing.read as read_module

    request = _CLI_SHAPED_REQUESTS[case]
    answered = handle_request(request, project=path_project)
    assert answered["response"] == "read", answered
    assert len(answered["value"]["identities"]) > 1, "a page hint needs more than one row"
    monkeypatch.setattr(read_module, "RESPONSE_BUDGET_WIRE_BYTES", measure(answered) - 1)
    monkeypatch.setattr(read_module, "HINT_PAGE_SIZE", 1)
    refused = handle_request(request, project=path_project)
    base = _assert_carries_the_sentence_once_at_its_end(refused)
    assert base == str(
        OverBudget(address=Address(layer="swc"), measured=read_module.Measured(**refused["measured"]), hints=())
    )
    assert refused["hints"] == [request | {"limit": 1}]
    assert str(path_project.resolved_project_root) not in refused["message"]


def test_a_join_ceiling_refusal_names_the_cli_and_still_carries_no_hint(
    path_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The join-ceiling refusal is the second refusal of the CLI's shape: the sentence, after its own words."""
    import swcraft_tools.editing.read as read_module

    _fan_out(path_project)
    rows = handle_request(PILOT_JOIN_REQUEST, project=path_project)["value"]["identities"]
    monkeypatch.setattr(read_module, "JOIN_ROW_CEILING", len(rows) - 1)
    for request in (PILOT_JOIN_REQUEST, PILOT_JOIN_REQUEST | {"limit": 1}):
        refused = handle_request(request, project=path_project)
        base = _assert_carries_the_sentence_once_at_its_end(refused)
        assert base.endswith("narrow the outer read or a joined read with `where`"), base
        assert "hints" not in refused
        assert str(path_project.resolved_project_root) not in refused["message"]


@pytest.mark.parametrize("case", sorted(_CLI_SHAPED_REQUESTS))
def test_the_refused_request_written_to_a_file_gives_the_cli_the_whole_answer(
    case: str, path_project: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The CLI, with no response budget, answers the request whole without `limit`/`after`; the budget stays lowered."""
    import swcraft_tools.editing.read as read_module

    request = _CLI_SHAPED_REQUESTS[case]
    whole = handle_request(request, project=path_project)["value"]["identities"]
    monkeypatch.setattr(read_module, "RESPONSE_BUDGET_WIRE_BYTES", 10)
    _assert_carries_the_sentence_once_at_its_end(handle_request(request | {"limit": 2}, project=path_project))
    assert _cli_answer(request, path_project, tmp_path, monkeypatch, capsys) == whole
    assert whole, "the equality is vacuous on an empty answer"


@pytest.mark.parametrize(
    "where",
    [
        pytest.param([{"field": "derived.partitions.core", "exists": True}], id="a-derived-not-asked-for"),
        pytest.param([{"field": "derived.effective_level.grade", "equals": "B"}], id="a-path-the-shape-lacks"),
    ],
)
def test_a_where_over_a_derived_value_is_refused_alike_by_the_cli(
    where: list[dict[str, Any]], path_project: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The two refusals of a derived clause: ``unknown-field`` here, exit 1 there, one sentence on both surfaces."""
    from swcraft_tools.cli import main

    body = {"address": {"layer": "swc"}, "form": "index", "derived": ["effective_level"], "where": where}
    refused = handle_request({"operation": "read", **body}, project=path_project)
    assert refused["error"] == "unknown-field", refused
    request = tmp_path / "refused-request.json"
    request.write_text(json.dumps(body), encoding="utf-8", newline="\n")
    manifest = str(Path(path_project.resolved_project_root) / "swcraft.yaml")
    monkeypatch.setattr("sys.argv", ["swcraft", "query", "--request", str(request), "--project", manifest])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 1
    assert capsys.readouterr().err.strip().endswith(refused["message"])


@pytest.fixture(scope="module")
def question_bench(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The agent task benchmark's small bench, copied once for the module."""
    return copy_agent_task_bench(tmp_path_factory.mktemp("absence")) / "swcraft.yaml"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {
                "address": {"layer": "swc"},
                "form": "count",
                "derived": ["partitions"],
                "where": [{"field": "derived.partitions.partition", "exists": False}],
            },
            id="no-partition",
        ),
        pytest.param(
            {
                "address": {"layer": "swc"},
                "members": "ports",
                "form": "count",
                "derived": ["unconnected"],
                "where": [{"field": "derived.unconnected.direction", "exists": False}],
            },
            id="not-unconnected",
        ),
        pytest.param(
            {
                "address": {"layer": "swc"},
                "form": "count",
                "derived": ["partitions"],
                "where": [{"field": "derived.partitions.core", "equals": None}],
            },
            id="equals-null-at-a-nullable-leaf",
        ),
    ],
)
def test_absence_over_a_set_valued_value_is_refused_on_both_surfaces(
    body: dict[str, Any], question_bench: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """An absence clause over a set-valued value is refused on both surfaces, not answered with a silent 0.

    A value with no value yields no row, so on the bench (7 components, 6 partitioned) it answered 0
    where 1 is right. ``unknown-field`` here; ``swcraft query --request`` exits 1 with the same sentence.
    """
    from swcraft_tools.cli import main

    assert (
        handle_request({"operation": "read", "address": {"layer": "swc"}, "form": "count"}, project=question_bench)[
            "count"
        ]
        == 7
    )
    refused = handle_request({"operation": "read", **body}, project=question_bench)
    assert refused["error"] == "unknown-field", refused
    assert "a computed value with no value yields no row" in refused["message"]
    assert "'none' is the layer's index minus these rows" in refused["message"]
    request = tmp_path / "absence.json"
    request.write_text(json.dumps({**body, "form": "index"}), encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.argv", ["swcraft", "query", "--request", str(request), "--project", str(question_bench)])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 1
    assert capsys.readouterr().err.strip().endswith(refused["message"])


@pytest.mark.parametrize(
    "clause",
    [
        pytest.param({"field": "derived.core.core", "exists": False}, id="exists-false"),
        pytest.param({"field": "derived.core.core", "equals": None}, id="equals-null"),
    ],
)
def test_absence_over_a_single_valued_value_is_answered(clause: dict[str, Any], question_bench: Path) -> None:
    """``core`` is one row per task, null where the core does not resolve: absence is a filter (answered 0)."""
    body = {"address": {"layer": "tasks"}, "form": "count", "derived": ["core"], "where": [clause]}
    answered = handle_request({"operation": "read", **body}, project=question_bench)
    assert answered["response"] == "count" and answered["count"] == 0, answered


@pytest.mark.parametrize(
    "request_body",
    [
        pytest.param({"address": {"layer": "swc"}}, id="layer-summary-by-default"),
        pytest.param({"address": {"layer": "swc"}, "form": "summary"}, id="layer-summary"),
        pytest.param({"address": {"layer": "swc", "element": "ExampleConsumerSWC"}}, id="element"),
        pytest.param(
            {"address": {"layer": "swc", "element": "ExampleConsumerSWC"}, "form": "index"}, id="element-index"
        ),
        pytest.param(
            {
                "address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "runnables"}]},
                "form": "index",
            },
            id="collection-index",
        ),
    ],
)
def test_a_refusal_the_cli_cannot_answer_does_not_name_it(
    request_body: dict[str, Any], path_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `summary` read, or an address below a layer: the CLI answers neither, so no sentence."""
    import swcraft_tools.editing.read as read_module

    monkeypatch.setattr(read_module, "RESPONSE_BUDGET_WIRE_BYTES", 10)
    refused = handle_request({"operation": "read", **request_body}, project=path_project)
    assert refused["error"] == "over-budget", refused
    assert "swcraft query" not in refused["message"]
    assert refused["message"].endswith("it is refused rather than truncated"), refused["message"]


def test_a_discover_refusal_does_not_name_the_cli(path_project: Any) -> None:
    """`discover` refuses through the same class, and the sentence is never added in its constructor."""
    refused = handle_request({"operation": "discover", "form": "full"}, project=path_project)
    assert refused["error"] == "over-budget", refused
    assert "swcraft query" not in refused["message"]


# The requirements layer's computed values through the engine


@pytest.fixture(scope="module")
def requirement_bench(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return copy_agent_task_bench(tmp_path_factory.mktemp("requirements")) / "swcraft.yaml"


def test_the_three_requirement_values_are_answered_together(requirement_bench: Path) -> None:
    """``allocation`` beside ``stale_links``: the product, one row per stale link, each carrying the allocation."""
    answer = _rows(requirement_bench, {"layer": "requirements"}, ["allocation", "stale_links"])
    assert answer["response"] == "read", answer
    rows = answer["value"]["identities"]
    assert [row["derived"]["stale_links"]["element"] for row in rows] == [
        "IBrakeDiagnostics",
        "IBrakeTemperature",
        "IThermalProtectionStatus",
    ]
    assert {row["element"] for row in rows} == {"REQ_BTP_TEMPERATURE_RANGE"}
    assert all(row["derived"]["allocation"]["allocated"] is True for row in rows)


def test_realised_by_pages_one_requirements_links_one_row_each(requirement_bench: Path) -> None:
    """``realised_by`` is one row per link, paged in order.

    ``where`` narrows to the requirement and ``limit``/``after`` page its links; the pages together are the unpaged
    answer, in order.
    """
    where = [{"field": "id", "equals": "REQ_BTP_TEMPERATURE_RANGE"}]
    whole = _rows(requirement_bench, {"layer": "requirements"}, ["realised_by"], where=where)
    assert whole["response"] == "read", whole
    expected = whole["value"]["identities"]
    assert len(expected) > 1, expected
    paged: list[dict[str, Any]] = []
    extra: dict[str, Any] = {"where": where, "limit": 1}
    while True:
        page = _rows(requirement_bench, {"layer": "requirements"}, ["realised_by"], **extra)
        assert page["response"] == "read", page
        assert len(page["value"]["identities"]) == 1, page
        paged.extend(page["value"]["identities"])
        if "next" not in page["page"]:
            break
        extra = {"where": where, "limit": 1, "after": page["page"]["next"]}
    assert paged == expected


@pytest.mark.parametrize("name", ["realised_by", "allocation", "stale_links"])
@pytest.mark.parametrize(
    ("address", "members"), [({"layer": "swc"}, None), ({"layer": "requirements"}, "derived_from")]
)
def test_a_requirement_value_elsewhere_is_a_schema_violation(
    requirement_bench: Path, name: str, address: dict[str, Any], members: str | None
) -> None:
    extra = {} if members is None else {"members": members}
    answer = _rows(requirement_bench, address, [name], **extra)
    assert answer["error"] == "schema-violation", answer
    assert "which is computed at the elements of layer 'requirements'" in answer["message"]

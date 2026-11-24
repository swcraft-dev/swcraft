"""`check_storage_portability` — `rule:storage.filename-case-collision`.

One rule about **file names**, never identities: two files of one layer directory whose names differ only in case
are one file on a case-insensitive filesystem. The check reports that tree and changes nothing. Not a finding: a file
name differing from the identity inside it, two identities differing only in case under file names that do not, and
the same name in two layer directories.

The message names the layer and the elements, never a file: pass D copies it verbatim into the protocol response.

On a case-insensitive filesystem the second write replaces the first, so the rule is silent there; every on-disk
case asks `filesystem_support.case_sensitive_under(tmp_path)` and asserts the finding or the silence accordingly,
and the grouping is witnessed on every host through `storage_portability_records`.

What this module does **not** reach: pass D through the engine (`test_editing_validation.py`); placement
directories (two `swc` groups differing only in case); Unicode normalisation, which collides on APFS as case does
and which `str.casefold()` does not see.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import swcraft_tools.validator as validator
import validator_package
import yaml
from conftest import COMPOSITIONS_MODEL_ROOT, REPO_ROOT, SAFETY_MODEL_ROOT, model_element_path
from filesystem_support import case_sensitive_under
from model_contract_fixtures import load_binding
from swcraft_tools.diagnostics import Subject
from swcraft_tools.editing.diagnosis import CONSISTENCY_VIOLATION, RULE_CODES
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import LayerFile, LayerScan
from swcraft_tools.validator import CONSISTENCY_RULES, check_cross_layer_consistency, model_view
from test_model_contract_conformance import (
    cross_element_layer_failures,
    emission_layers,
    load_contract,
)

RULE = "storage.filename-case-collision"

#: The rule's summary, written out by hand: the expected value may not be read
#: from the document it checks.
SUMMARY = (
    "Two files of one layer directory whose names differ only in case, "
    "which a case-insensitive filesystem stores as one."
)

#: The fifteen `layer:` identifiers, by hand, in the contract's layer order. A
#: mapping set is one file of one layer directory like every other element, so the
#: rule reaches it.
DESIGN_APPLIES_TO = [
    "layer:swc",
    "layer:interfaces",
    "layer:connectors",
    "layer:application_types",
    "layer:implementation_types",
    "layer:base_types",
    "layer:requirements",
    "layer:compositions",
    "layer:tasks",
    "layer:sequences",
    "layer:hardware",
    "layer:mapping_sets",
    "layer:units",
    "layer:compu_methods",
    "layer:data_constraints",
]

EXAMPLES_MODEL_ROOT = REPO_ROOT / "examples" / "basic" / "model"
HARDWARE_FIXTURE_MODEL_ROOT = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "hardware" / "model"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reader(identities: dict[str, str | None]) -> tuple[Callable[[str, str], str | None], list[tuple[str, str]]]:
    """An identity reader over *identities*, and the list of every question it was asked."""
    asked: list[tuple[str, str]] = []

    def identity_of(layer: str, path: str) -> str | None:
        asked.append((layer, path))
        return identities[path]

    return identity_of, asked


def _records(stored: dict[str, dict[str, str | None]]) -> list[Any]:
    """`storage_portability_records` over a listing whose identities are given with it."""
    identities = {path: identity for files in stored.values() for path, identity in files.items()}
    identity_of, _ = _reader(identities)
    return validator.storage_portability_records({layer: tuple(files) for layer, files in stored.items()}, identity_of)


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _primitive(model_root: Path, filename: str, name: str) -> Path:
    """A base type file named *filename*, in the base types' own directory."""
    path = model_element_path(model_root, Path(filename).stem, kind="base_type")
    _write(path, {"name": name, "kind": "base_type", "size": 8, "encoding": "NONE"})
    return path


def _base_types_dir(model_root: Path) -> Path:
    """The directory `_primitive` writes into."""
    return model_element_path(model_root, "probe", kind="base_type").parent


def _storage_records(model_root: Path) -> tuple[Any, ...]:
    return tuple(validator.check_storage_portability(str(model_root)).records)


def _message_carries_no_path(message: str, *segments: str) -> None:
    """No file name, no separator and none of *segments*: the no-storage-path boundary."""
    assert ".yaml" not in message, message
    assert "/" not in message and "\\" not in message, message
    for segment in segments:
        assert segment not in message, (segment, message)


# ---------------------------------------------------------------------------
# 1. The grouping, over a listing handed to it — witnessed on every host
# ---------------------------------------------------------------------------


class TestTheGroupingOverAListing:
    def test_two_names_differing_only_in_case_are_one_finding_naming_both_elements(self) -> None:
        (record,) = _records(
            {
                "implementation_types": {
                    "data_types/implementation/float32.yaml": "float32",
                    "data_types/implementation/Float32.yaml": "Float32",
                }
            }
        )

        assert record.rule == RULE
        assert record.subjects == (
            Subject("implementation_types", "float32"),
            Subject("implementation_types", "Float32"),
        )
        assert record.target is None
        assert record.message == (
            "Elements 'float32' and 'Float32' of layer `implementation_types` are stored under file names that "
            "differ only in case, which a case-insensitive filesystem holds as one file; rename one of the two files."
        )

    def test_three_names_differing_only_in_case_are_one_finding(self) -> None:
        (record,) = _records(
            {
                "interfaces": {
                    "interfaces/ispeed.yaml": "ispeed",
                    "interfaces/ISpeed.yaml": "ISpeed",
                    "interfaces/ISPEED.yaml": "ISPEED",
                }
            }
        )

        assert record.subjects == tuple(Subject("interfaces", name) for name in ("ispeed", "ISpeed", "ISPEED"))
        assert record.message == (
            "Elements 'ispeed', 'ISpeed' and 'ISPEED' of layer `interfaces` are stored under file names that differ "
            "only in case, which a case-insensitive filesystem holds as one file; rename all but one of the 3 files."
        )

    def test_one_identity_declared_by_both_files_is_one_subject(self) -> None:
        """Two files claiming one name are `data-type.duplicate-name` too; here they are one subject."""
        (record,) = _records(
            {"implementation_types": {"data_types/implementation/a.yaml": "A", "data_types/implementation/A.yaml": "A"}}
        )

        assert record.subjects == (Subject("implementation_types", "A"),)
        assert record.message.startswith("Element 'A' of layer `implementation_types` is stored under file names")
        assert record.message.endswith("rename one of the two files.")

    def test_the_same_name_in_two_layer_directories_is_not_a_finding(self) -> None:
        assert (
            _records(
                {
                    "implementation_types": {"data_types/implementation/a.yaml": "a"},
                    "interfaces": {"interfaces/A.yaml": "A"},
                }
            )
            == []
        )

    def test_a_file_name_differing_from_its_identity_is_not_a_finding(self) -> None:
        assert _records({"implementation_types": {"data_types/implementation/float32.yaml": "Float32"}}) == []

    def test_two_identities_differing_only_in_case_under_distinct_file_names_are_not_a_finding(self) -> None:
        """The shape a suffixed write leaves: the files differ, so nothing collides."""
        assert (
            _records(
                {
                    "implementation_types": {
                        "data_types/implementation/Float32.yaml": "Float32",
                        "data_types/implementation/float32~1.yaml": "float32",
                    }
                }
            )
            == []
        )

    def test_a_nesting_layer_groups_within_one_placement_directory_only(self) -> None:
        """`swc/g/X.yaml` beside `swc/g/x.yaml` collides; a case-only placement pair is a stated limit."""
        (record,) = _records({"swc": {"swc/sensors/Probe.yaml": "Probe", "swc/sensors/probe.yaml": "probe"}})
        assert record.subjects == (Subject("swc", "Probe"), Subject("swc", "probe"))

        assert _records({"swc": {"swc/a/Probe.yaml": "Probe", "swc/A/probe.yaml": "probe"}}) == []
        assert _records({"swc": {"swc/a/Probe.yaml": "Probe", "swc/b/probe.yaml": "probe"}}) == []

    def test_no_message_carries_a_file_name_or_a_path_segment(self) -> None:
        (record,) = _records({"swc": {"swc/sensors/Probe.yaml": "Probe", "swc/sensors/probe.yaml": "probe"}})

        _message_carries_no_path(record.message, "sensors", "Probe.yaml", "probe.yaml")

    def test_a_file_declaring_no_identity_is_not_an_element(self) -> None:
        """The readers refuse such a file before any check runs; here it simply holds no element."""
        assert (
            _records(
                {
                    "implementation_types": {
                        "data_types/implementation/a.yaml": None,
                        "data_types/implementation/A.yaml": "A",
                    }
                }
            )
            == []
        )

    def test_an_identity_is_read_only_for_a_file_in_a_colliding_group(self) -> None:
        """A conforming model pays the listing and no parse."""
        identity_of, asked = _reader(
            {
                "data_types/implementation/uint8.yaml": "uint8",
                "data_types/implementation/float32.yaml": "float32",
                "data_types/implementation/Float32.yaml": "Float32",
            }
        )

        validator.storage_portability_records(
            {
                "implementation_types": (
                    "data_types/implementation/uint8.yaml",
                    "data_types/implementation/float32.yaml",
                    "data_types/implementation/Float32.yaml",
                )
            },
            identity_of,
        )

        assert asked == [
            ("implementation_types", "data_types/implementation/float32.yaml"),
            ("implementation_types", "data_types/implementation/Float32.yaml"),
        ]

    def test_layers_are_reported_in_the_registrys_declared_order_and_groups_in_listing_order(self) -> None:
        records = _records(
            {
                "hardware": {"hardware/core0.yaml": "core0", "hardware/CORE0.yaml": "CORE0"},
                "implementation_types": {
                    "data_types/implementation/b.yaml": "b",
                    "data_types/implementation/a.yaml": "a",
                    "data_types/implementation/B.yaml": "B",
                    "data_types/implementation/A.yaml": "A",
                },
            }
        )

        assert [record.subjects for record in records] == [
            (Subject("implementation_types", "b"), Subject("implementation_types", "B")),
            (Subject("implementation_types", "a"), Subject("implementation_types", "A")),
            (Subject("hardware", "core0"), Subject("hardware", "CORE0")),
        ]

    def test_a_layer_the_registry_does_not_declare_is_not_read(self) -> None:
        assert _records({"synthetic": {"synthetic/a.yaml": "a", "synthetic/A.yaml": "A"}}) == []


# ---------------------------------------------------------------------------
# 2. Over the disk — probe-gated where the tree can only exist on one kind of host
# ---------------------------------------------------------------------------


class TestTheCheckOverTheDisk:
    def test_a_case_only_pair_is_reported_where_the_filesystem_can_hold_it_and_silent_where_it_cannot(
        self, tmp_path: Path
    ) -> None:
        model_root = tmp_path / "model"
        _primitive(model_root, "float32.yaml", "float32")
        _primitive(model_root, "Float32.yaml", "Float32")
        on_disk = sorted(path.name for path in _base_types_dir(model_root).iterdir())

        if case_sensitive_under(tmp_path):
            assert on_disk == ["Float32.yaml", "float32.yaml"]
            (record,) = _storage_records(model_root)
            assert record.rule == RULE
            # `scan_layer`'s POSIX order: `F` (0x46) sorts before `f` (0x66).
            assert record.subjects == (Subject("base_types", "Float32"), Subject("base_types", "float32"))
            _message_carries_no_path(record.message, str(tmp_path))
        else:
            # The loss the rule exists to prevent has already happened: the second
            # write replaced the first, and nothing is left to report.
            assert on_disk == ["float32.yaml"]
            assert yaml.safe_load((_base_types_dir(model_root) / "float32.yaml").read_text(encoding="utf-8"))[
                "name"
            ] == ("Float32")
            assert _storage_records(model_root) == ()

    def test_three_case_variants_are_one_finding_where_they_can_exist(self, tmp_path: Path) -> None:
        model_root = tmp_path / "model"
        for filename, name in (("ispeed.yaml", "ispeed"), ("ISpeed.yaml", "ISpeed"), ("ISPEED.yaml", "ISPEED")):
            _primitive(model_root, filename, name)

        if case_sensitive_under(tmp_path):
            (record,) = _storage_records(model_root)
            assert {subject.element for subject in record.subjects} == {"ispeed", "ISpeed", "ISPEED"}
            assert record.message.endswith("rename all but one of the 3 files.")
        else:
            assert len(list(_base_types_dir(model_root).iterdir())) == 1
            assert _storage_records(model_root) == ()

    def test_the_same_name_in_two_layer_directories_is_silent(self, tmp_path: Path) -> None:
        model_root = tmp_path / "model"
        _primitive(model_root, "Probe.yaml", "Probe")
        _write(model_root / "hardware" / "probe.yaml", {"name": "probe", "kind": "processor"})

        assert _storage_records(model_root) == ()

    def test_a_file_name_differing_from_its_identity_is_silent(self, tmp_path: Path) -> None:
        model_root = tmp_path / "model"
        _primitive(model_root, "float32.yaml", "Float32")

        assert _storage_records(model_root) == ()

    def test_a_suffixed_file_beside_its_case_variant_is_silent(self, tmp_path: Path) -> None:
        model_root = tmp_path / "model"
        _primitive(model_root, "Float32.yaml", "Float32")
        _primitive(model_root, "float32~1.yaml", "float32")

        assert _storage_records(model_root) == ()

    def test_the_disk_half_asks_each_colliding_file_by_its_joined_path_and_its_layers_own_identity_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Witnessed on every host: the listing is replaced, so no case-sensitive filesystem is needed.

        ``requirements`` is identified by ``id``, not ``name``. A reader asking
        ``name`` of every layer, or a wrong path join, would make both files declare
        nothing and silence the rule everywhere with every other test green.
        """
        model_root = tmp_path / "model"
        listed = ("requirements/REQ-a.yaml", "requirements/req-A.yaml")
        declared = {"REQ-a.yaml": "REQ-a", "req-A.yaml": "req-A"}
        asked: list[tuple[Path, str]] = []

        def listing(root: Path, layer: str, *, registry: Any = None) -> LayerScan:
            files = tuple(LayerFile(path=Path(root) / path) for path in listed) if layer == "requirements" else ()
            return LayerScan(present=True, files=files)

        def identity_of(path: Path, identity_field: str) -> str | None:
            asked.append((path, identity_field))
            return declared[path.name]

        monkeypatch.setattr(model_view, "scan_layer", listing)
        monkeypatch.setattr(model_view, "declared_identity", identity_of)

        (record,) = _storage_records(model_root)

        assert record.subjects == (Subject("requirements", "REQ-a"), Subject("requirements", "req-A"))
        assert asked == [(model_root / listed[0], "id"), (model_root / listed[1], "id")]

    def test_a_case_only_requirements_pair_is_reported_by_its_ids_where_the_filesystem_can_hold_it(
        self, tmp_path: Path
    ) -> None:
        model_root = tmp_path / "model"
        for filename, identity in (("REQ-a.yaml", "REQ-a"), ("req-A.yaml", "req-A")):
            _write(
                model_root / "requirements" / filename,
                {
                    "id": identity,
                    "title": identity,
                    "description": identity,
                    "req_type": "req",
                    "version": 1,
                    "source": "architecture",
                },
            )

        if case_sensitive_under(tmp_path):
            (record,) = _storage_records(model_root)
            assert record.subjects == (Subject("requirements", "REQ-a"), Subject("requirements", "req-A"))
            _message_carries_no_path(record.message, str(tmp_path))
        else:
            assert len(list((model_root / "requirements").iterdir())) == 1
            assert _storage_records(model_root) == ()

    @pytest.mark.parametrize(
        "model_root",
        [EXAMPLES_MODEL_ROOT, SAFETY_MODEL_ROOT, COMPOSITIONS_MODEL_ROOT, HARDWARE_FIXTURE_MODEL_ROOT],
        ids=["examples-basic", "safety", "compositions", "hardware-core-files-named-otherwise"],
    )
    def test_every_authored_model_is_silent(self, model_root: Path) -> None:
        assert _storage_records(model_root) == ()


# ---------------------------------------------------------------------------
# 3. The composition
# ---------------------------------------------------------------------------


def _composed_members() -> list[str]:
    """The names in the `for check in (...)` tuple `check_cross_layer_consistency` runs, in order."""
    return list(validator_package.composed_checks())


class TestTheComposition:
    def test_the_check_is_the_composed_tuples_last_member(self) -> None:
        """The composed tuple's tail, in order.

        A member appended last adds no coupling: no finding an earlier member reports
        moves. Exceptions keep the writer's order: `check_application_type_mapping`
        follows the member whose findings it abstains on; the six up-front checks from
        `check_port_com_specs` on (with `check_runnable_symbols` right after it) sit
        after `check_operation_events` in the order the writer runs their twins;
        `check_xml_representable_text` follows them, where the writer refuses such a
        text while emitting; `check_name_space_collisions` and
        `check_termination_and_restart` follow, because their writer twins run after
        emission.
        """
        members = _composed_members()

        assert members[-25:] == [
            "check_hardware_capability",
            "check_storage_portability",
            "check_base_type_native_declarations",
            "check_mapping_set_references",
            "check_application_type_mapping",
            "check_application_type_double_mapping",
            "check_operation_events",
            "check_port_com_specs",
            "check_runnable_symbols",
            "check_queued_receivers",
            "check_disabled_modes",
            "check_queued_reads",
            "check_service_composition_ports",
            "check_conversion_units",
            "check_xml_representable_text",
            "check_name_space_collisions",
            "check_termination_and_restart",
            "check_runnable_costs",
            "check_requirement_traceability",
            "check_inter_runnable_variables",
            "check_exclusive_areas",
            "check_data_events",
            "check_unknown_keys",
            "check_implicit_reads",
            "check_async_calls",
        ]

    def test_the_aggregate_reports_it_where_the_tree_can_exist(self, tmp_path: Path) -> None:
        model_root = tmp_path / "model"
        _write(model_root / "hardware" / "CPU.yaml", {"name": "CPU", "kind": "processor"})
        _primitive(model_root, "spare.yaml", "spare")
        _primitive(model_root, "Spare.yaml", "Spare")

        reported = [record.rule for record in check_cross_layer_consistency(str(model_root)).records]

        assert (RULE in reported) is case_sensitive_under(tmp_path)


# ---------------------------------------------------------------------------
# 4. Registered everywhere a cross-element rule has to be, once
# ---------------------------------------------------------------------------


class TestTheRuleIsRegisteredEverywhereOnce:
    def test_the_rule_is_in_the_closed_inventory_with_the_designs_summary(self) -> None:
        declared = {entry["id"]: entry for entry in load_contract()["rules"]}

        assert CONSISTENCY_RULES[RULE] == SUMMARY == declared[f"rule:{RULE}"]["summary"]

    def test_the_protocol_maps_it_to_consistency_violation_in_a_group_of_its_own(self) -> None:
        assert RULE_CODES[RULE].code == CONSISTENCY_VIOLATION
        assert RULE_CODES[RULE].group == RULE

    def test_the_contract_declares_it_cross_element_over_the_fifteen_layers(self) -> None:
        declared = {entry["id"]: entry for entry in load_contract()["rules"]}
        entry = declared[f"rule:{RULE}"]

        assert entry["family"] == "cross-element"
        assert entry["appliesTo"] == DESIGN_APPLIES_TO
        assert entry["appliesTo"] == [layer["id"] for layer in load_contract()["layers"]]

    def test_the_binding_names_the_records_function(self) -> None:
        assert load_binding()["rules"][f"rule:{RULE}"] == {
            "module": "swcraft_tools.validator.storage",
            "callable": "storage_portability_records",
        }

    def test_the_emission_site_yields_every_declared_layer(self) -> None:
        """The loop variable over the registry's declared layers is read as those layers."""
        observed = emission_layers()
        sites = observed[f"rule:{RULE}"]

        assert sites and all(site == frozenset(current_registry().layer_names) for site in sites), sites
        assert cross_element_layer_failures(load_contract(), observed) == []

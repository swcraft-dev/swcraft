"""`rule:storage.reserved-device-name`.

Windows reserves `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9` and `LPT1`–`LPT9` as device names: no file whose name
before the first `.` is one of them, in any case, can exist on a Windows volume. Each pinned on every host unless
it says otherwise:

1. **The kernel refusal**: `model_registry.segment_refusal` refuses a file segment of a reserved base name in one
   exact sentence naming the rule; near misses stay legal; a *directory* segment is not asked.
2. **The finding**: `storage_portability_records` mints one finding per such file, naming the layer and the
   element and no storage path; on disk it is probe-gated.
3. **The editing protocol** refuses a `create` of such an identity at bind time, and a `remove` of one already
   stored, so the protocol cannot perform the repair the finding asks for.
4. **Both importers skip** the element with a reason naming the rule: `import model` goes on, `import component`
   aborts with nothing written.
5. **The chain**: a referrer of a skipped element keeps the name it spelled, `check-model` reports it and
   `generate arxml` refuses the project.
6. **What does not change**: the `create_*` mutations accept the name, and the writer-refusals oracle stays.

What this module does **not** reach: a Windows host (where `create_*`'s outcome is the filesystem's); a name with a
trailing space before its extension and the superscript digits, which Windows also treats as devices; and a `swc`
group directory of a reserved name.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import swcraft_tools.validator as validator
import yaml
from conftest import REPO_ROOT
from filesystem_support import stores_a_device_named_file_under
from swcraft_tools.arxml_import import run_import
from swcraft_tools.cli import main
from swcraft_tools.component_exchange.export import export_component
from swcraft_tools.component_exchange.import_ import LossyComponentImportError, import_component
from swcraft_tools.diagnostics import Subject
from swcraft_tools.editing.audit import _recorded_element_address
from swcraft_tools.editing.diagnosis import CONSISTENCY_VIOLATION, RULE_CODES
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.model_registry import (
    _RESERVED_DEVICE_NAMES,
    DEVICE_NAME_SPELLINGS,
    current_registry,
    path_refusal,
    reserved_device_name,
    segment_refusal,
)
from swcraft_tools.validator import CONSISTENCY_RULES, check_cross_layer_consistency
from swcraft_tools.writer.mutations import create_interface

RULE = "storage.reserved-device-name"

#: The kernel's sentence for ``interfaces identity 'CON'``, written out by hand:
#: it reaches a user through the editing protocol, and the expected value may
#: not be read from the function it checks.
CON_REFUSAL = (
    "interfaces identity 'CON' names a device Windows reserves (CON): no file whose name before the first '.' "
    "is CON, PRN, AUX, NUL, COM1-COM9 or LPT1-LPT9, in any case, can exist on Windows, so the element cannot "
    "be stored there, and one already stored under such a name is renamed by hand "
    "(rule:storage.reserved-device-name)"
)

#: The importers' skip reason for a `CON` element, by hand for the same reason.
CON_SKIP_REASON = (
    "its name before the first '.' is CON, a device name Windows reserves, so no file of that name can exist "
    "on Windows; not imported (rule:storage.reserved-device-name)"
)

#: Every reserved device name and a case form of each, with what the base name answers.
RESERVED = [
    ("CON", "CON"),
    ("con", "CON"),
    ("Con", "CON"),
    ("PRN", "PRN"),
    ("prn", "PRN"),
    ("AUX", "AUX"),
    ("aUx", "AUX"),
    ("NUL", "NUL"),
    ("nul", "NUL"),
    *((f"COM{digit}", f"COM{digit}") for digit in range(1, 10)),
    ("com1", "COM1"),
    ("Com5", "COM5"),
    *((f"LPT{digit}", f"LPT{digit}") for digit in range(1, 10)),
    ("lpt9", "LPT9"),
    # The base name is the part before the FIRST `.`, whatever follows it.
    ("CON.v2", "CON"),
    ("nul.cfg.bak", "NUL"),
]

#: Names that look like a device name and are not one.
NEAR_MISSES = [
    "CONSOLE",
    "CON_1",
    "ACON",
    "COM10",
    "COM0",
    "LPT0",
    "LPT10",
    "NULL",
    "AUXILIARY",
    "PRNT",
    "A.CON",
    # The collision suffix `free_storage_path` composes is an ordinary name on Windows.
    "CON~1",
    # A superscript digit is Windows' too, and a stated limit here: not ASCII, not asked.
    "COM¹",
]


def _init_project(tmp_path: Path, name: str = "proj") -> Path:
    """A fresh project through the real ``swcraft init`` path."""
    target = tmp_path / name
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(target)]
        main()
    finally:
        sys.argv = argv
    return target


def _run_cli(*argv: str) -> int:
    saved = sys.argv
    try:
        sys.argv = ["swcraft", *argv]
        try:
            main()
        except SystemExit as exit_info:
            return int(exit_info.code or 0)
        return 0
    finally:
        sys.argv = saved


def _assert_no_path_in(message: str, *segments: str) -> None:
    assert ".yaml" not in message, message
    assert "/" not in message and "\\" not in message, message
    for segment in segments:
        assert segment not in message, (segment, message)


# ---------------------------------------------------------------------------
# 1. The kernel refusal
# ---------------------------------------------------------------------------


class TestTheKernelRefusal:
    @pytest.mark.parametrize(("name", "device"), RESERVED, ids=[name for name, _ in RESERVED])
    def test_a_reserved_base_name_is_refused_as_a_file_segment(self, name: str, device: str) -> None:
        assert reserved_device_name(name) == device
        refusal = segment_refusal(name, "interfaces identity")
        assert refusal == (
            f"interfaces identity '{name}' names a device Windows reserves ({device}): no file whose name before "
            "the first '.' is CON, PRN, AUX, NUL, COM1-COM9 or LPT1-LPT9, in any case, can exist on Windows, so "
            "the element cannot be stored there, and one already stored under such a name is renamed by hand "
            "(rule:storage.reserved-device-name)"
        )

    def test_the_sentence_is_the_one_a_user_reads(self) -> None:
        assert segment_refusal("CON", "interfaces identity") == CON_REFUSAL

    @pytest.mark.parametrize("name", NEAR_MISSES)
    def test_a_near_miss_stays_legal(self, name: str) -> None:
        assert reserved_device_name(name) is None
        assert segment_refusal(name, "interfaces identity") is None

    def test_the_refusal_does_not_depend_on_the_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The rule is about where a project can be checked out, so every platform refuses.

        A tripwire for a platform gate read **at call time**; it does not reach one
        captured at import (a module constant).
        """
        for platform in ("linux", "darwin", "win32"):
            monkeypatch.setattr(sys, "platform", platform)
            assert segment_refusal("con", "tasks identity") is not None

    def test_the_identity_guard_every_layer_composes_through_refuses_it(self) -> None:
        registry = current_registry()
        for layer in registry.layer_names:
            refusal = path_refusal(registry.storage(layer), "Nul", {"group": "g"}, layer=layer)
            assert refusal is not None and refusal.endswith("(rule:storage.reserved-device-name)"), (layer, refusal)

    def test_a_directory_segment_is_not_asked(self) -> None:
        """A stated limit, the sibling rule's: a stored `swc/con/` group must keep reading."""
        assert segment_refusal("con", "swc group", directory=True) is None

    def test_the_spelled_set_is_the_set_the_check_asks(self) -> None:
        """The refusal and the rule summary list the set from one spelling, and it is the checked set.

        Each ``XN-XM`` range is expanded by hand here, so widening the frozenset
        without the spelling (or the spelling without the frozenset) fails.
        """
        expanded: set[str] = set()
        for spelling in DEVICE_NAME_SPELLINGS:
            ranged = re.fullmatch(r"([A-Z]+)(\d)-\1(\d)", spelling)
            if ranged is None:
                expanded.add(spelling)
            else:
                prefix, first, last = ranged.group(1), int(ranged.group(2)), int(ranged.group(3))
                expanded |= {f"{prefix}{digit}" for digit in range(first, last + 1)}
        assert expanded == _RESERVED_DEVICE_NAMES


# ---------------------------------------------------------------------------
# 2. The finding
# ---------------------------------------------------------------------------


def _records(stored: dict[str, dict[str, str | None]]) -> tuple[list[Any], list[tuple[str, str]]]:
    identities = {path: identity for files in stored.values() for path, identity in files.items()}
    asked: list[tuple[str, str]] = []

    def identity_of(layer: str, path: str) -> str | None:
        asked.append((layer, path))
        return identities[path]

    records = validator.storage_portability_records(
        {layer: tuple(files) for layer, files in stored.items()}, identity_of
    )
    return [record for record in records if record.rule == RULE], asked


class TestTheFindingOverAListing:
    def test_one_finding_per_file_naming_the_element_and_no_file(self) -> None:
        (record,), _ = _records({"interfaces": {"interfaces/CON.yaml": "CON", "interfaces/ISpeed.yaml": "ISpeed"}})

        assert record.rule == RULE
        assert record.subjects == (Subject("interfaces", "CON"),)
        assert record.target is None
        assert record.message == (
            "Element 'CON' of layer `interfaces` is stored under a file name Windows reserves for the device CON, "
            "which no Windows filesystem can hold; rename the element and its file."
        )
        _assert_no_path_in(record.message, "CON.yaml")

    def test_the_file_name_decides_and_the_identity_is_what_is_named(self) -> None:
        """A file `com3.yaml` declaring `Speed` is reported, about `Speed`; `CON~1.yaml` declaring `CON` is not."""
        (record,), _ = _records(
            {
                "implementation_types": {
                    "data_types/implementation/com3.yaml": "Speed",
                    "data_types/implementation/CON~1.yaml": "CON",
                }
            }
        )

        assert record.subjects == (Subject("implementation_types", "Speed"),)
        assert "device COM3" in record.message

    def test_a_nesting_layer_is_read_within_its_placement_directory(self) -> None:
        (record,), _ = _records({"swc": {"swc/sensors/Aux.yaml": "Aux", "swc/con/Probe.yaml": "Probe"}})

        assert record.subjects == (Subject("swc", "Aux"),)
        _assert_no_path_in(record.message, "sensors")

    def test_an_identity_is_read_only_for_a_reserved_file(self) -> None:
        """A conforming model still pays the listing and no parse."""
        _, asked = _records({"tasks": {"tasks/Task_10ms.yaml": "Task_10ms", "tasks/lpt1.yaml": "lpt1"}})

        assert asked == [("tasks", "tasks/lpt1.yaml")]

    def test_a_file_declaring_no_identity_holds_no_element(self) -> None:
        records, _ = _records({"tasks": {"tasks/nul.yaml": None}})
        assert records == []

    def test_every_layer_is_read_in_the_registrys_order(self) -> None:
        records, _ = _records(
            {
                "mapping_sets": {"data_types/mapping_sets/PRN.yaml": "PRN"},
                "swc": {"swc/g/CON.yaml": "CON"},
                "hardware": {"hardware/AUX.yaml": "AUX"},
            }
        )
        assert [record.subjects[0].layer for record in records] == ["swc", "hardware", "mapping_sets"]

    def test_the_rule_is_a_validator_rule_and_a_consistency_violation(self) -> None:
        assert CONSISTENCY_RULES[RULE] == (
            "A file of a layer directory whose name before the first '.' is a device name Windows reserves "
            "(CON, PRN, AUX, NUL, COM1-COM9, LPT1-LPT9, in any case), which no Windows filesystem can hold."
        )
        assert RULE_CODES[RULE].code == CONSISTENCY_VIOLATION


def _sr_interface(name: str) -> dict[str, Any]:
    return {"name": name, "kind": "sender_receiver", "data_elements": [{"name": "v", "implementation_type": "uint8"}]}


class TestTheFindingOverTheDisk:
    """Probe-gated, never skipped: the tree can be built only where the name is an ordinary file."""

    def test_check_model_reports_a_stored_file_of_a_reserved_name(self, tmp_path: Path) -> None:
        project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
        model = project / "model"
        (model / "interfaces" / "NUL.yaml").write_text(
            yaml.safe_dump(_sr_interface("NUL"), sort_keys=False), encoding="utf-8", newline="\n"
        )

        records = [record for record in check_cross_layer_consistency(str(model)).records if record.rule == RULE]

        if stores_a_device_named_file_under(tmp_path):
            (record,) = records
            assert record.subjects == (Subject("interfaces", "NUL"),)
            _assert_no_path_in(record.message, str(model), model.as_posix())
            # ... and the CLI gate exits non-zero on it.
            assert _run_cli("check-model", "--project", str(project / "swcraft.yaml")) == 1
        else:
            # The write opened a device and stored nothing: nothing to report.
            assert records == []

    def test_a_stored_file_refuses_every_change_set_where_it_can_exist(self, tmp_path: Path) -> None:
        project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
        (project / "model" / "interfaces" / "NUL.yaml").write_text(
            yaml.safe_dump(_sr_interface("NUL"), sort_keys=False), encoding="utf-8", newline="\n"
        )

        response = _dispatch(project, _create("interfaces", _sr_interface("IFresh")))

        if stores_a_device_named_file_under(tmp_path):
            assert response["error"] == "consistency-violation", response
            assert "Element 'NUL' of layer `interfaces`" in response["message"], response
            assert response["addresses"] == [{"layer": "interfaces", "element": "NUL"}], response
            _assert_no_path_in(response["message"], str(project))
        else:
            assert response["response"] == "change_set" and response["status"] == "staged", response


# ---------------------------------------------------------------------------
# 3. The editing protocol
# ---------------------------------------------------------------------------


def _create(layer: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "create", "address": {"layer": layer, "element": value["name"]}, "value": value}


def _dispatch(project_root: Path, *operations: dict[str, Any]) -> dict[str, Any]:
    return handle_request(
        {
            "operation": "change_set",
            "baseline": baseline_token(project_root / "model"),
            "intent": "a test change set",
            "operations": list(operations),
        },
        project=project_root,
    )


class TestTheEditingProtocolRefusesTheIdentity:
    def test_a_create_of_a_reserved_identity_is_refused_at_bind_time(self, tmp_path: Path) -> None:
        project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
        before = sorted(path.name for path in (project / "model" / "interfaces").iterdir())

        response = _dispatch(project, _create("interfaces", _sr_interface("CON")))

        assert response["error"] == "schema-violation", response
        assert response["message"] == CON_REFUSAL
        assert response["addresses"] == [{"layer": "interfaces", "element": "CON"}]
        assert sorted(path.name for path in (project / "model" / "interfaces").iterdir()) == before

    def test_a_remove_of_a_stored_reserved_element_is_a_refusal_and_never_an_exception(self, tmp_path: Path) -> None:
        """An ``update`` or ``remove`` asks the identity guard too, so the stored element is refused.

        The protocol therefore cannot perform the repair the finding asks for (a stated
        limit: the file is renamed by hand). The answer is the protocol's own refusal, not
        an uncaught ``PathSegmentError``, whether or not the host could store the file.
        """
        project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
        stored = project / "model" / "interfaces" / "CON.yaml"
        stored.write_text(yaml.safe_dump(_sr_interface("CON"), sort_keys=False), encoding="utf-8", newline="\n")

        response = _dispatch(project, {"operation": "remove", "address": {"layer": "interfaces", "element": "CON"}})

        assert response["error"] == "schema-violation", response
        assert response["message"] == CON_REFUSAL
        assert response["addresses"] == [{"layer": "interfaces", "element": "CON"}]
        if stores_a_device_named_file_under(tmp_path):
            assert stored.is_file()

    def test_the_audit_reader_does_not_classify_a_reserved_element_address(self) -> None:
        """Fail-closed: a record naming ``CON`` (a create committed before the rule existed) loses its address."""
        assert _recorded_element_address({"layer": "interfaces", "element": "CON"}) is None
        assert _recorded_element_address({"layer": "interfaces", "element": "CONSOLE"}) == ("interfaces", "CONSOLE")

    def test_a_near_miss_is_staged(self, tmp_path: Path) -> None:
        project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")

        response = _dispatch(project, _create("interfaces", _sr_interface("CONSOLE")))

        assert response["response"] == "change_set" and response["status"] == "staged", response


# ---------------------------------------------------------------------------
# 4 and 5. Both importers skip, and the chain behind the skip
# ---------------------------------------------------------------------------

BASIC_ARXML = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "arxml_golden" / "basic-4.2.2.arxml"


def _tampered_basic_export(tmp_path: Path) -> Path:
    """The `examples/basic` export with its sender-receiver interface renamed `CON` throughout."""
    text = BASIC_ARXML.read_text(encoding="utf-8")
    assert "<SHORT-NAME>IExample</SHORT-NAME>" in text, "the fixture declares the interface this test renames"
    tampered = tmp_path / "reserved.arxml"
    tampered.write_text(text.replace("IExample", "CON"), encoding="utf-8", newline="\n")
    return tampered


COMPOSITE_ARXML = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "arxml_golden" / "composite-4.2.2.arxml"
INIT_VALUES_ARXML = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "arxml" / "init_values.arxml"


def _renamed(source: Path, tmp_path: Path, old: str, new: str) -> Path:
    """*source* with the SHORT-NAME and path segment *old* spelled *new* wherever the export names it."""
    text, count = re.subn(rf"(?<=[>/]){re.escape(old)}(?=[<\"/])", new, source.read_text(encoding="utf-8"))
    assert count, f"the fixture declares {old}"
    tampered = tmp_path / f"reserved-{new}.arxml"
    tampered.write_text(text, encoding="utf-8", newline="\n")
    return tampered


def _skip_reason(device: str) -> str:
    return CON_SKIP_REASON.replace("is CON,", f"is {device},")


#: The data description elements fixture tree: a row naming it imports SWCraft's own build of it.
ELEMENTS_TREE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "data_description" / "model"

#: One element of each layer ``import model`` writes, renamed to a device name:
#: (fixture, declared name, reserved name, layer directory, skip path, skip tag).
EVERY_LAYER = [
    (
        BASIC_ARXML,
        "int16",
        "CON",
        "data_types/implementation",
        "/DataTypes/Implementation/CON",
        "IMPLEMENTATION-DATA-TYPE",
    ),
    (
        COMPOSITE_ARXML,
        "PhysicalTypes",
        "LPT1",
        "data_types/mapping_sets",
        "/DataTypes/MappingSets/LPT1",
        "DATA-TYPE-MAPPING-SET",
    ),
    (BASIC_ARXML, "IExample", "CON", "interfaces", "/PortInterfaces/CON", "SENDER-RECEIVER-INTERFACE"),
    (BASIC_ARXML, "ExampleConsumerSWC", "AUX", "swc", "/ComponentTypes/AUX", "APPLICATION-SW-COMPONENT-TYPE"),
    (BASIC_ARXML, "Example_to_Consumer", "NUL", "connectors", "/ComponentTypes/Root/NUL", "ASSEMBLY-SW-CONNECTOR"),
    (BASIC_ARXML, "Root", "PRN", "compositions", "/ComponentTypes/PRN", "COMPOSITION-SW-COMPONENT-TYPE"),
    (ELEMENTS_TREE, "ProbeUnitBare", "NUL", "data_types/units", "/DataTypes/Units/NUL", "UNIT"),
    (ELEMENTS_TREE, "ProbeTable", "PRN", "data_types/compu_methods", "/DataTypes/CompuMethods/PRN", "COMPU-METHOD"),
    (ELEMENTS_TREE, "ProbeLimits", "AUX", "data_types/constraints", "/DataTypes/Constraints/AUX", "DATA-CONSTR"),
]


def _source_file(source: Path, tmp_path: Path) -> Path:
    """*source* itself, or SWCraft's own build of a model tree."""
    if not source.is_dir():
        return source
    from swcraft_tools.writer import generate_arxml

    tree = tmp_path / "tree"
    shutil.copytree(source, tree)
    built = tmp_path / "build.arxml"
    generate_arxml(str(tree), str(built), version="4.2.2")
    return built


class TestImportModelSkipsTheElement:
    @pytest.mark.parametrize(
        ("source", "declared", "reserved", "layer", "path", "tag"),
        EVERY_LAYER,
        ids=[row[3] for row in EVERY_LAYER],
    )
    def test_every_written_layer_skips_with_the_declarations_path_and_tag(
        self, tmp_path: Path, source: Path, declared: str, reserved: str, layer: str, path: str, tag: str
    ) -> None:
        project = _init_project(tmp_path)

        report = run_import(
            str(_renamed(_source_file(source, tmp_path), tmp_path, declared, reserved)), str(project / "swcraft.yaml")
        )

        skips = [(entry.path, entry.tag, entry.reason) for entry in report.skip_entries if RULE in entry.reason]
        assert skips == [(path, tag, _skip_reason(reserved))]
        assert f"1 × {tag}: {_skip_reason(reserved)}" in report.render()
        assert not [
            stored
            for stored in (project / "model" / layer).rglob("*.yaml")
            if stored.name.split(".")[0].casefold() == reserved.casefold()
        ]
        # The import writes no requirement at all, so no file links the component
        # the project never receives -- or any other.
        assert list((project / "model" / "requirements").glob("*.yaml")) == []

    def test_a_skipped_interface_takes_its_radix_line_with_it(self, tmp_path: Path) -> None:
        """No report line is about an element in no written file."""
        project = _init_project(tmp_path)

        report = run_import(
            str(_renamed(INIT_VALUES_ARXML, tmp_path, "IScalars", "AUX")), str(project / "swcraft.yaml")
        )

        assert [entry.path for entry in report.skip_entries if RULE in entry.reason] == ["/Acme/Chassis/Interfaces/AUX"]
        assert sorted(report.init_radix_lines) == [
            "/Acme/Chassis/Interfaces/IComposites/point: init read from 0x0a, written decimal",
            "/Acme/Chassis/Interfaces/IComposites/triple: init read from 0x10, written decimal",
        ]

    def test_a_skipped_interface_takes_its_unresolved_reference_with_it(self, tmp_path: Path) -> None:
        declared = (
            "<SHORT-NAME>value</SHORT-NAME>\n"
            '              <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/float32<'
        )
        text = BASIC_ARXML.read_text(encoding="utf-8")
        assert text.count(declared) == 1
        unresolving = tmp_path / "unresolving.arxml"
        # An application-type DEST, so the absent target stays an unresolved reference:
        # an absent IMPLEMENTATION-DATA-TYPE in this role is imported as an external
        # type stub instead, and there would be no record to narrow.
        unresolving.write_text(
            text.replace(
                declared,
                declared.replace("/DataTypes/Implementation/float32", "/DataTypes/Nowhere").replace(
                    "IMPLEMENTATION-DATA-TYPE", "APPLICATION-PRIMITIVE-DATA-TYPE"
                ),
            ),
            encoding="utf-8",
            newline="\n",
        )

        kept = run_import(str(unresolving), str(_init_project(tmp_path, "kept") / "swcraft.yaml"))
        report = run_import(
            str(_renamed(unresolving, tmp_path, "IExample", "CON")), str(_init_project(tmp_path) / "swcraft.yaml")
        )

        # Non-vacuity: the same export with a legal name carries the record.
        assert [record.target for record in kept.unresolved_references] == ["/DataTypes/Nowhere"]
        assert [entry.path for entry in report.skip_entries if RULE in entry.reason] == ["/PortInterfaces/CON"]
        assert report.unresolved_references == []

    def test_the_element_is_skipped_with_a_reason_naming_the_rule(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)

        report = run_import(str(_tampered_basic_export(tmp_path)), str(project / "swcraft.yaml"))

        (skip,) = [entry for entry in report.skip_entries if "reserved-device-name" in entry.reason]
        assert (skip.path, skip.tag, skip.reason) == (
            "/PortInterfaces/CON",
            "SENDER-RECEIVER-INTERFACE",
            CON_SKIP_REASON,
        )
        assert report.imported_interfaces == 1
        assert f"1 × SENDER-RECEIVER-INTERFACE: {CON_SKIP_REASON}" in report.render()
        assert not [
            path for path in (project / "model" / "interfaces").iterdir() if path.name.lower().startswith("con")
        ]

    def test_a_dry_run_reports_the_same_skip(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)

        report = run_import(str(_tampered_basic_export(tmp_path)), str(project / "swcraft.yaml"), dry_run=True)

        assert [entry.reason for entry in report.skip_entries if "reserved-device-name" in entry.reason] == [
            CON_SKIP_REASON
        ]

    def test_an_export_declaring_no_such_name_reports_no_such_line(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)

        report = run_import(str(BASIC_ARXML), str(project / "swcraft.yaml"))

        assert not [entry for entry in report.skip_entries if "reserved-device-name" in entry.reason]
        assert report.imported_interfaces == 2


class TestTheChainBehindTheSkip:
    """A referrer of a skipped element is kept with the name it spelled; the gate then refuses."""

    def test_the_referrer_is_kept_check_model_reports_it_and_generate_arxml_refuses(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        run_import(str(_tampered_basic_export(tmp_path)), str(project / "swcraft.yaml"))
        model = project / "model"

        ports = [
            port
            for path in sorted((model / "swc").rglob("*.yaml"), key=lambda path: path.as_posix())
            for port in yaml.safe_load(path.read_text(encoding="utf-8")).get("ports", [])
        ]
        assert sorted(port["name"] for port in ports if port["interface"] == "CON") == ["pOut", "rIn"]

        records = check_cross_layer_consistency(str(model)).records
        assert sorted((record.rule, record.subjects[0].element) for record in records) == [
            ("port.undeclared-interface", "ExampleConsumerSWC"),
            ("port.undeclared-interface", "ExampleSWC"),
        ]
        assert _run_cli("check-model", "--project", str(project / "swcraft.yaml")) == 1

        assert _run_cli("generate", "arxml", "--project", str(project / "swcraft.yaml")) == 1
        assert not (project / "dist").exists()


class TestImportComponentAborts:
    def test_a_reserved_name_in_the_closure_aborts_with_nothing_written(self, tmp_path: Path) -> None:
        source = REPO_ROOT / "examples" / "basic" / "model"
        exported = tmp_path / "ExampleSWC.arxml"
        export_component(str(source), "ExampleSWC", str(exported))
        text = exported.read_text(encoding="utf-8")
        assert "IExample" in text
        tampered = tmp_path / "reserved-component.arxml"
        tampered.write_text(text.replace("IExample", "CON"), encoding="utf-8", newline="\n")
        project = _init_project(tmp_path)
        model = project / "model"
        before = sorted(path.relative_to(model).as_posix() for path in model.rglob("*"))

        with pytest.raises(LossyComponentImportError) as refusal:
            import_component(str(tampered), str(model))

        assert f"SENDER-RECEIVER-INTERFACE /PortInterfaces/CON: {CON_SKIP_REASON}" in refusal.value.skip_lines
        assert sorted(path.relative_to(model).as_posix() for path in model.rglob("*")) == before

    def test_a_reserved_component_aborts_with_nothing_written(self, tmp_path: Path) -> None:
        source = REPO_ROOT / "examples" / "basic" / "model"
        exported = tmp_path / "ExampleSWC.arxml"
        export_component(str(source), "ExampleSWC", str(exported))
        tampered = _renamed(exported, tmp_path, "ExampleSWC", "AUX")
        project = _init_project(tmp_path)
        model = project / "model"
        before = sorted(path.relative_to(model).as_posix() for path in model.rglob("*"))

        with pytest.raises(LossyComponentImportError) as refusal:
            import_component(str(tampered), str(model))

        assert [line for line in refusal.value.skip_lines if "reserved-device-name" in line] == [
            f"APPLICATION-SW-COMPONENT-TYPE /ComponentTypes/AUX: {_skip_reason('AUX')}"
        ]
        assert sorted(path.relative_to(model).as_posix() for path in model.rglob("*")) == before


# ---------------------------------------------------------------------------
# 6. What does not change
# ---------------------------------------------------------------------------

#: SHA-256 of the frozen `writer_refusals.json` oracle's data: its canonical JSON with the prose keys (``note``,
#: ``reason``, any ``*_note``) left out, so a prose edit keeps it. It is never regenerated, so a new kernel refusal
#: must not reach it: the `create_*` mutations deliberately accept what the guard refuses. It moves only with a
#: re-recording of the fixture that moves no verdict (a seed's file content a contract change no longer loads).
WRITER_REFUSALS_SHA256 = "9268e9b398110b3c09ad950e42a4dcf37a3d6802ebcfac252587534c3540eae5"

#: The annotation keys of `writer_refusals.json`, which carry prose and no recorded value.
_PROSE_KEYS = frozenset({"note", "reason"})


def _without_prose(value: Any) -> Any:
    """Return *value* with every mapping's prose keys (:data:`_PROSE_KEYS` and any ``*_note``) left out."""
    if isinstance(value, dict):
        return {
            key: _without_prose(member)
            for key, member in value.items()
            if key not in _PROSE_KEYS and not key.endswith("_note")
        }
    if isinstance(value, list):
        return [_without_prose(member) for member in value]
    return value


def _data_digest(text: str) -> str:
    """Return the SHA-256 of *text*'s JSON without its prose, canonically serialised."""
    canonical = json.dumps(_without_prose(json.loads(text)), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TestTheCreateMutationsAreUnchanged:
    def test_create_interface_accepts_the_name_exactly_as_before(self, tmp_path: Path) -> None:
        model = tmp_path / "model"
        (model / "interfaces").mkdir(parents=True)

        if stores_a_device_named_file_under(tmp_path, "NUL.yaml"):
            written = create_interface(str(model), "NUL", [{"name": "v", "implementation_type": "uint8"}])
            assert Path(written).name == "NUL.yaml"
            assert yaml.safe_load((model / "interfaces" / "NUL.yaml").read_text(encoding="utf-8"))["name"] == "NUL"
        else:
            # Where the name opens a device the outcome is the filesystem's `O_EXCL`
            # answer, not SWCraft's. What is asserted is only that no kernel refusal
            # was added in front of it.
            try:
                create_interface(str(model), "NUL", [{"name": "v", "implementation_type": "uint8"}])
            except OSError:
                pass
            except ValueError as refusal:
                assert "reserved-device-name" not in str(refusal), refusal

    def test_the_frozen_writer_oracle_does_not_move(self) -> None:
        frozen = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "recorded_oracles" / "writer_refusals.json"
        assert _data_digest(frozen.read_text(encoding="utf-8")) == WRITER_REFUSALS_SHA256

    def test_the_digest_ignores_prose_and_sees_a_moved_verdict(self) -> None:
        frozen = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "recorded_oracles" / "writer_refusals.json"
        data = json.loads(frozen.read_text(encoding="utf-8"))
        collision = data["collisions"][0]["cases"]["path_held_by_another_identity"]
        collision["reason"] = "reworded"
        data["note"] = "reworded"
        assert _data_digest(json.dumps(data)) == WRITER_REFUSALS_SHA256
        collision["accepted"] = not collision["accepted"]
        assert _data_digest(json.dumps(data)) != WRITER_REFUSALS_SHA256

"""``build_needs_json()`` and ``generate_needs()``: the ``needs.json`` the requirements team loads.

The unit oracle is hand-written expected text, compared byte for byte: the envelope, the needs
SWCraft publishes (architecture requirements and linked architecture elements, never an incoming
requirement), the two kinds of link split into confirmed and unconfirmed by ``trace.link_confirmed``,
and the delivery named in ``current_version``; also the element ids, the selection of linked
elements, each link once in file order, the ``[[`` rule, the ``asil`` rule and the collision
refusal. The refusals are separate classes, each asserting that nothing is written.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest
from conftest import REPO_ROOT, _swc, _write, element_path
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import stored_element_files
from swcraft_tools.writer import generate_needs, needs_export
from swcraft_tools.writer.needs_export import NeedsExportRefused, build_needs_json

_ONE_DATA_ELEMENT = [{"name": "value", "implementation_type": "UInt8"}]
_MODULE = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "writer" / "needs_export.py"


def _req_file(model_root, file_stem: str, req_id: str, **extra) -> None:
    """One requirement under a file name of the test's choosing; an architecture `req` at version 1 by default."""
    _write(
        pathlib.Path(model_root) / "requirements" / f"{file_stem}.yaml",
        {
            "id": req_id,
            "title": extra.pop("title", f"Requirement {file_stem}"),
            "description": extra.pop("description", f"The text of {file_stem}."),
            "req_type": extra.pop("req_type", "req"),
            "version": extra.pop("version", 1),
            "source": extra.pop("source", "architecture"),
            **extra,
        },
    )


def _incoming(model_root, req_id: str, version: int = 1, baseline: str | None = "2.3", **extra) -> None:
    """One incoming requirement, as an import of delivery *baseline* leaves it."""
    fields = {"source": "external", "version": version, **extra}
    if baseline is not None:
        fields["baseline"] = baseline
    _req_file(model_root, req_id.replace(".", "_").replace("[", "_").replace("]", "_"), req_id, **fields)


def _hardware(model_root, name: str, kind: str, **extra) -> None:
    _write(element_path(model_root, "hardware", name), {"name": name, "kind": kind, **extra})


def _build(model_root, project: str = "Demo") -> str:
    return build_needs_json(str(model_root), project)


def _needs(model_root) -> dict[str, dict]:
    document = json.loads(_build(model_root))
    return document["versions"][document["current_version"]]["needs"]


def _refusal(model_root) -> str:
    with pytest.raises(NeedsExportRefused) as refused:
        _build(model_root)
    return str(refused.value)


# ---------------------------------------------------------------------------
# What the file holds (success)
# ---------------------------------------------------------------------------


class TestTheArchitectureRequirements:
    def test_req_and_spec_with_their_level_and_both_kinds_of_derived_from(self, tmp_path):
        """`asil` is written for a declared non-`QM` level only; `QM` and `QM(B)` write none."""
        _incoming(tmp_path, "SR-3", version=2)
        _incoming(tmp_path, "SR-4", version=2)
        _req_file(
            tmp_path,
            "AR_1",
            "AR-1",
            title="Brake within 50 ms",
            description="The brake shall act within 50 ms.",
            asil="B",
            derived_from=["SR-3@2", "SR-4@1", "swc:Brake"],
        )
        _req_file(tmp_path, "AS_1", "AS-1", title="Brake frame", description="", req_type="spec", asil="QM")
        _req_file(tmp_path, "AR_2", "AR-2", title="Decomposed", description="Text.", asil="QM(B)")
        _swc(tmp_path, "Brake", description="The brake controller.")

        assert _build(tmp_path) == (
            "{\n"
            '  "current_version": "2.3",\n'
            '  "project": "Demo",\n'
            '  "versions": {\n'
            '    "2.3": {\n'
            '      "needs": {\n'
            '        "AR-1": {\n'
            '          "asil": "B",\n'
            '          "content": "The brake shall act within 50 ms.",\n'
            '          "derived_from": [\n'
            '            "SR-3",\n'
            '            "SWC-Brake"\n'
            "          ],\n"
            '          "derived_from_unconfirmed": [\n'
            '            "SR-4"\n'
            "          ],\n"
            '          "id": "AR-1",\n'
            '          "swcraft_path": "requirements/AR_1.yaml",\n'
            '          "tags": [],\n'
            '          "title": "Brake within 50 ms",\n'
            '          "type": "arch_req"\n'
            "        },\n"
            '        "AR-2": {\n'
            '          "content": "Text.",\n'
            '          "id": "AR-2",\n'
            '          "swcraft_path": "requirements/AR_2.yaml",\n'
            '          "tags": [],\n'
            '          "title": "Decomposed",\n'
            '          "type": "arch_req"\n'
            "        },\n"
            '        "AS-1": {\n'
            '          "content": "",\n'
            '          "id": "AS-1",\n'
            '          "swcraft_path": "requirements/AS_1.yaml",\n'
            '          "tags": [],\n'
            '          "title": "Brake frame",\n'
            '          "type": "arch_spec"\n'
            "        },\n"
            '        "SWC-Brake": {\n'
            '          "content": "The brake controller.",\n'
            '          "id": "SWC-Brake",\n'
            '          "swcraft_path": "swc/group/Brake.yaml",\n'
            '          "tags": [\n'
            '            "swc"\n'
            "          ],\n"
            '          "title": "Brake",\n'
            '          "type": "arch_element"\n'
            "        }\n"
            "      },\n"
            '      "needs_amount": 4\n'
            "    }\n"
            "  }\n"
            "}\n"
        )

    @pytest.mark.parametrize("level", ["B(D)", "D", "A"])
    def test_any_other_declared_level_is_written_as_declared(self, tmp_path, level):
        _req_file(tmp_path, "AR_1", "AR-1", asil=level)

        assert _needs(tmp_path)["AR-1"]["asil"] == level

    def test_a_requirement_declaring_no_level_writes_no_field(self, tmp_path):
        _req_file(tmp_path, "AR_1", "AR-1")

        assert "asil" not in _needs(tmp_path)["AR-1"]

    def test_a_derived_from_to_another_architecture_requirement_is_confirmed_at_its_version(self, tmp_path):
        _req_file(tmp_path, "AR_1", "AR-1", version=3)
        _req_file(tmp_path, "AR_2", "AR-2", derived_from=["AR-1@3", "AR-1"])

        need = _needs(tmp_path)["AR-2"]

        assert need["derived_from"] == ["AR-1"]
        assert "derived_from_unconfirmed" not in need

    def test_an_architecture_requirement_whose_own_id_holds_a_dot_is_written_under_that_id(self, tmp_path):
        _req_file(tmp_path, "AR_1", "AR.1", title="Dotted")

        assert _needs(tmp_path)["AR.1"]["id"] == "AR.1"


class TestTheArchitectureElements:
    def test_an_element_with_confirmed_and_unconfirmed_satisfies(self, tmp_path):
        _incoming(tmp_path, "SR-1", version=2)
        _incoming(tmp_path, "SR-2", version=3)
        _write(
            element_path(tmp_path, "tasks", "Task10ms"),
            {
                "name": "Task10ms",
                "period": 0.01,
                "priority": 1,
                "partition": "P0",
                "description": "The 10 ms task.",
                "satisfies": ["SR-1@2", "SR-2@1", "SR-2"],
            },
        )

        assert _build(tmp_path) == (
            "{\n"
            '  "current_version": "2.3",\n'
            '  "project": "Demo",\n'
            '  "versions": {\n'
            '    "2.3": {\n'
            '      "needs": {\n'
            '        "TASK-Task10ms": {\n'
            '          "content": "The 10 ms task.",\n'
            '          "id": "TASK-Task10ms",\n'
            '          "satisfies": [\n'
            '            "SR-1"\n'
            "          ],\n"
            '          "satisfies_unconfirmed": [\n'
            '            "SR-2"\n'
            "          ],\n"
            '          "swcraft_path": "tasks/Task10ms.yaml",\n'
            '          "tags": [\n'
            '            "task"\n'
            "          ],\n"
            '          "title": "Task10ms",\n'
            '          "type": "arch_element"\n'
            "        }\n"
            "      },\n"
            '      "needs_amount": 1\n'
            "    }\n"
            "  }\n"
            "}\n"
        )

    def test_a_port_and_a_runnable_are_needs_of_their_own_and_the_component_is_not(self, tmp_path):
        """No nesting: the component carries no link and no `derived_from` names it, so it is not written."""
        _incoming(tmp_path, "SR-1")
        _swc(
            tmp_path,
            "Brake",
            description="The brake controller.",
            ports=[
                {"name": "pTemp", "direction": "require", "interface": "I", "satisfies": ["SR-1@1"]},
                {"name": "pOther", "direction": "provide", "interface": "I"},
            ],
            runnables=[{"name": "Run10ms", "description": "Runs.", "satisfies": ["SR-1"]}, {"name": "RunInit"}],
        )

        assert _build(tmp_path) == (
            "{\n"
            '  "current_version": "2.3",\n'
            '  "project": "Demo",\n'
            '  "versions": {\n'
            '    "2.3": {\n'
            '      "needs": {\n'
            '        "PORT-Brake-pTemp": {\n'
            '          "content": "",\n'
            '          "id": "PORT-Brake-pTemp",\n'
            '          "satisfies": [\n'
            '            "SR-1"\n'
            "          ],\n"
            '          "swcraft_path": "swc/group/Brake.yaml",\n'
            '          "tags": [\n'
            '            "port"\n'
            "          ],\n"
            '          "title": "Brake.pTemp",\n'
            '          "type": "arch_element"\n'
            "        },\n"
            '        "RUN-Brake-Run10ms": {\n'
            '          "content": "Runs.",\n'
            '          "id": "RUN-Brake-Run10ms",\n'
            '          "satisfies_unconfirmed": [\n'
            '            "SR-1"\n'
            "          ],\n"
            '          "swcraft_path": "swc/group/Brake.yaml",\n'
            '          "tags": [\n'
            '            "runnable"\n'
            "          ],\n"
            '          "title": "Brake.Run10ms",\n'
            '          "type": "arch_element"\n'
            "        }\n"
            "      },\n"
            '      "needs_amount": 2\n'
            "    }\n"
            "  }\n"
            "}\n"
        )

    def test_a_component_linking_itself_is_written_beside_its_linked_member(self, tmp_path):
        _incoming(tmp_path, "SR-1")
        _swc(
            tmp_path,
            "Brake",
            satisfies=["SR-1@1"],
            ports=[{"name": "pTemp", "direction": "require", "interface": "I", "satisfies": ["SR-1@1"]}],
        )

        needs = _needs(tmp_path)

        assert sorted(needs) == ["PORT-Brake-pTemp", "SWC-Brake"]
        assert needs["SWC-Brake"]["satisfies"] == ["SR-1"]
        assert needs["SWC-Brake"]["tags"] == ["swc"]

    @pytest.mark.parametrize(
        ("layer", "element", "tag", "need_id"),
        [
            (
                "interfaces",
                {"name": "ISr", "kind": "sender_receiver", "data_elements": _ONE_DATA_ELEMENT},
                "interface",
                "IF-ISr",
            ),
            (
                "interfaces",
                {"name": "ICs", "kind": "client_server", "operations": [{"name": "Op"}]},
                "interface",
                "IF-ICs",
            ),
            ("compositions", {"name": "Vehicle", "title": "Vehicle"}, "composition", "COMP-Vehicle"),
            ("tasks", {"name": "T", "period": 0.01, "priority": 1, "partition": "P0"}, "task", "TASK-T"),
            ("hardware", {"name": "Cpu", "kind": "processor"}, "processor", "HW-Cpu"),
            ("hardware", {"name": "Core0", "kind": "core", "threshold": 0.7}, "core", "HW-Core0"),
            (
                "hardware",
                {"name": "Ram", "kind": "memory", "type": "RAM", "size": 10, "accessible": ["Core0"], "threshold": 0.8},
                "memory",
                "HW-Ram",
            ),
            ("hardware", {"name": "P0", "kind": "partition", "core": "Core0"}, "partition", "HW-P0"),
        ],
    )
    def test_each_kind_has_its_tag_and_its_layers_prefix(self, tmp_path, layer, element, tag, need_id):
        _incoming(tmp_path, "SR-1")
        _write(element_path(tmp_path, layer, element["name"]), {**element, "satisfies": ["SR-1@1"]})

        need = _needs(tmp_path)[need_id]

        assert (need["type"], need["tags"], need["title"], need["satisfies"]) == (
            "arch_element",
            [tag],
            element["name"],
            ["SR-1"],
        )

    def test_a_derived_from_element_target_is_written_with_no_link_of_its_own(self, tmp_path):
        _req_file(tmp_path, "AR_1", "AR-1", derived_from=["hardware:Core0"])
        _hardware(tmp_path, "Core0", "core", threshold=0.7, description="The safety core.")
        _hardware(tmp_path, "Core1", "core", threshold=0.7)

        needs = _needs(tmp_path)

        assert needs["HW-Core0"] == {
            "content": "The safety core.",
            "id": "HW-Core0",
            "swcraft_path": "hardware/Core0.yaml",
            "tags": ["core"],
            "title": "Core0",
            "type": "arch_element",
        }
        assert "HW-Core1" not in needs

    def test_a_derived_from_naming_no_element_is_unconfirmed_and_publishes_no_need(self, tmp_path):
        """A confirmed link always resolves: an element reference is confirmed only when the element is in the model."""
        _req_file(tmp_path, "AR_1", "AR-1", derived_from=["swc:Ghost", "swc:Brake", "hardware:Ghost"])
        _swc(tmp_path, "Brake")

        needs = _needs(tmp_path)

        assert needs["AR-1"]["derived_from"] == ["SWC-Brake"]
        assert needs["AR-1"]["derived_from_unconfirmed"] == ["SWC-Ghost", "HW-Ghost"]
        assert sorted(needs) == ["AR-1", "SWC-Brake"]

    def test_an_incoming_requirements_element_form_derived_from_brings_no_element(self, tmp_path):
        """Only an architecture requirement's `derived_from` selects an element: an incoming one is not published."""
        _incoming(tmp_path, "SR-1", derived_from=["swc:Brake"])
        _swc(tmp_path, "Brake")

        assert _needs(tmp_path) == {}

    def test_an_unlinked_element_is_not_written(self, tmp_path):
        _swc(tmp_path, "Brake")
        _hardware(tmp_path, "Cpu", "processor")

        assert _needs(tmp_path) == {}

    def test_an_element_whose_only_members_are_element_form_is_written_with_no_link(self, tmp_path):
        _hardware(tmp_path, "Cpu", "processor", satisfies=["swc:Brake"])

        assert _needs(tmp_path)["HW-Cpu"] == {
            "content": "",
            "id": "HW-Cpu",
            "swcraft_path": "hardware/Cpu.yaml",
            "tags": ["processor"],
            "title": "Cpu",
            "type": "arch_element",
        }

    def test_an_element_name_that_is_no_identifier_is_written_as_it_stands(self, tmp_path):
        _incoming(tmp_path, "SR-1")
        _hardware(tmp_path, "Core 0", "core", threshold=0.7, satisfies=["SR-1@1"])

        assert _needs(tmp_path)["HW-Core 0"]["title"] == "Core 0"

    def test_members_whose_names_join_alike_get_distinct_ids(self, tmp_path):
        _incoming(tmp_path, "SR-1")
        _swc(tmp_path, "A_B", ports=[{"name": "C", "direction": "provide", "interface": "I", "satisfies": ["SR-1"]}])
        _swc(tmp_path, "A", ports=[{"name": "B_C", "direction": "provide", "interface": "I", "satisfies": ["SR-1"]}])

        assert sorted(_needs(tmp_path)) == ["PORT-A-B_C", "PORT-A_B-C"]

    def test_members_whose_names_join_alike_with_a_hyphen_collide(self, tmp_path):
        """The id joins with ``-``, which a name may hold: ``A-B`` + ``C`` and ``A`` + ``B-C`` are one id, refused."""
        _incoming(tmp_path, "SR-1")
        _swc(tmp_path, "A-B", ports=[{"name": "C", "direction": "provide", "interface": "I", "satisfies": ["SR-1"]}])
        _swc(tmp_path, "A", ports=[{"name": "B-C", "direction": "provide", "interface": "I", "satisfies": ["SR-1"]}])

        assert _refusal(tmp_path) == (
            "the id PORT-A-B-C would be taken by more than one need: element PORT-A-B-C, element PORT-A-B-C"
        )


class TestTheIncomingRequirements:
    def test_no_incoming_requirement_is_a_need_while_links_name_both_kinds(self, tmp_path):
        _incoming(tmp_path, "SR-1")
        _incoming(tmp_path, "SR-9", baseline=None, stub=True)
        _req_file(tmp_path, "AR_1", "AR-1", derived_from=["SR-1@1", "SR-9@1"])
        _swc(tmp_path, "Brake", satisfies=["SR-1@1", "SR-9@1"])

        needs = _needs(tmp_path)

        assert sorted(needs) == ["AR-1", "SWC-Brake"]
        assert needs["AR-1"]["derived_from"] == ["SR-1"]
        assert needs["SWC-Brake"]["satisfies"] == ["SR-1"]

    def test_a_link_to_a_stub_at_its_current_version_is_unconfirmed(self, tmp_path):
        _incoming(tmp_path, "SR-9", baseline=None, stub=True)
        _swc(tmp_path, "Brake", satisfies=["SR-9@1"])

        need = _needs(tmp_path)["SWC-Brake"]

        assert need["satisfies_unconfirmed"] == ["SR-9"]
        assert "satisfies" not in need

    @pytest.mark.parametrize("member", ["SR-1", "SR-1@2", "SR-1@x", "SR-1@01", "GHOST-1@1"])
    def test_every_other_requirement_form_link_is_unconfirmed(self, tmp_path, member):
        """A bare id, a stale `@N`, a suffix that is not a version, an id no file declares."""
        _incoming(tmp_path, "SR-1", version=1)
        _swc(tmp_path, "Brake", satisfies=[member])

        assert _needs(tmp_path)["SWC-Brake"] == {
            "content": "",
            "id": "SWC-Brake",
            "satisfies_unconfirmed": [member.partition("@")[0]],
            "swcraft_path": "swc/group/Brake.yaml",
            "tags": ["swc"],
            "title": "Brake",
            "type": "arch_element",
        }

    def test_a_link_to_an_id_two_files_declare_is_unconfirmed(self, tmp_path):
        _incoming(tmp_path, "SR-1")
        _req_file(tmp_path, "SR_1_copy", "SR-1", source="external", baseline="2.3")
        _swc(tmp_path, "Brake", satisfies=["SR-1@1"])

        assert _needs(tmp_path)["SWC-Brake"]["satisfies_unconfirmed"] == ["SR-1"]


class TestTheLinkLists:
    def test_a_link_listed_twice_is_written_once_in_file_order(self, tmp_path):
        _incoming(tmp_path, "SR-1")
        _incoming(tmp_path, "SR-2")
        _swc(tmp_path, "Brake", satisfies=["SR-2@1", "SR-1@1", "SR-2@1"])

        assert _needs(tmp_path)["SWC-Brake"]["satisfies"] == ["SR-2", "SR-1"]

    def test_an_id_a_confirmed_member_names_is_not_repeated_among_the_unconfirmed(self, tmp_path):
        _incoming(tmp_path, "SR-1", version=2)
        _swc(tmp_path, "Brake", satisfies=["SR-1@1", "SR-1@2"])

        need = _needs(tmp_path)["SWC-Brake"]

        assert need["satisfies"] == ["SR-1"]
        assert "satisfies_unconfirmed" not in need

    def test_a_member_naming_nothing_writes_nothing(self, tmp_path):
        _req_file(tmp_path, "AR_1", "AR-1", derived_from=["@3", "swc:", "foo:bar"])
        _swc(tmp_path, "Brake", satisfies=["@3", "swc:Brake"])

        needs = _needs(tmp_path)

        assert "derived_from" not in needs["AR-1"]
        assert "derived_from_unconfirmed" not in needs["AR-1"]
        assert needs["SWC-Brake"] == {
            "content": "",
            "id": "SWC-Brake",
            "swcraft_path": "swc/group/Brake.yaml",
            "tags": ["swc"],
            "title": "Brake",
            "type": "arch_element",
        }

    def test_the_internal_version_never_reaches_the_file(self, tmp_path):
        _incoming(tmp_path, "SR-1", version=7, revision="r12", document="SRS")
        _req_file(tmp_path, "AR_1", "AR-1", version=4, derived_from=["SR-1@7"])

        text = _build(tmp_path)

        for absent in ("@7", '"version"', "revision", "r12", "SRS", '"source"', '"baseline"', '"stub"'):
            assert absent not in text, absent


class TestTheDelivery:
    def test_one_delivery_is_the_current_version_and_the_one_version(self, tmp_path):
        _incoming(tmp_path, "SR-1", baseline="2.3")
        _incoming(tmp_path, "SR-2", baseline="2.3")

        document = json.loads(_build(tmp_path))

        assert document["current_version"] == "2.3"
        assert list(document["versions"]) == ["2.3"]

    def test_no_incoming_requirement_names_the_empty_version(self, tmp_path):
        _req_file(tmp_path, "AR_1", "AR-1")

        document = json.loads(_build(tmp_path))

        assert (document["current_version"], list(document["versions"])) == ("", [""])

    def test_only_unnamed_baselines_name_the_empty_version(self, tmp_path):
        _incoming(tmp_path, "SR-1", baseline=None)
        _incoming(tmp_path, "SR-2", baseline=None)

        assert json.loads(_build(tmp_path))["current_version"] == ""

    def test_an_empty_baseline_counts_as_none(self, tmp_path):
        _incoming(tmp_path, "SR-1", baseline="")
        assert json.loads(_build(tmp_path))["current_version"] == ""

        _incoming(tmp_path, "SR-2", baseline="2.3")
        assert _refusal(tmp_path) == (
            "the imported requirements come from more than one delivery (2.3: 1 requirement, none: 1); "
            "import the current delivery, then generate"
        )

    def test_stubs_are_not_counted(self, tmp_path):
        _incoming(tmp_path, "SR-1", baseline="2.3")
        _incoming(tmp_path, "SR-9", baseline=None, stub=True)
        _incoming(tmp_path, "SR-8", baseline="1.0", stub=True)

        assert json.loads(_build(tmp_path))["current_version"] == "2.3"

    def test_an_empty_model_is_an_empty_version_of_the_project(self, tmp_path):
        assert _build(tmp_path, "Vehicle") == (
            '{\n  "current_version": "",\n  "project": "Vehicle",\n  "versions": {\n'
            '    "": {\n      "needs": {},\n      "needs_amount": 0\n    }\n  }\n}\n'
        )


class TestTheBytes:
    def test_keys_are_sorted_at_every_level(self, tmp_path):
        _incoming(tmp_path, "SR-1")
        _swc(tmp_path, "Zeta", satisfies=["SR-1@1"])
        _swc(tmp_path, "Alpha", satisfies=["SR-1"])
        text = _build(tmp_path)

        def _sorted_everywhere(value) -> bool:
            if isinstance(value, dict):
                return list(value) == sorted(value) and all(_sorted_everywhere(item) for item in value.values())
            if isinstance(value, list):
                return all(_sorted_everywhere(item) for item in value)
            return True

        assert _sorted_everywhere(json.loads(text))
        assert text.index('"SWC-Alpha"') < text.index('"SWC-Zeta"')

    def test_non_ascii_text_is_written_as_utf8_not_escaped(self, tmp_path):
        _req_file(tmp_path, "AR_1", "AR-1", title="Bremse über 600 °C")

        assert '"title": "Bremse über 600 °C"' in _build(tmp_path)

    def test_generate_needs_writes_the_one_file_with_lf_line_ends_creating_its_folder(self, tmp_path):
        model = tmp_path / "model"
        _req_file(model, "AR_1", "AR-1", description="Line one.\nLine two.\n")
        output = tmp_path / "dist" / "deep" / "needs.json"

        returned = generate_needs(str(model), str(output), "Demo")

        assert returned == str(output)
        assert output.read_bytes() == _build(model).encode("utf-8")
        assert b"\r\n" not in output.read_bytes()
        assert sorted(path.name for path in output.parent.iterdir()) == ["needs.json"]


class TestEachNeedNamesItsFile:
    """``swcraft_path``: the model-root-relative storage name of the file declaring the need."""

    def test_each_kind_names_the_file_that_declares_it(self, tmp_path):
        """A port's and a runnable's file is their component's; a requirement's is its own, whatever its id."""
        _incoming(tmp_path, "SR-1")
        _req_file(tmp_path, "AR_1", "AR-1", derived_from=["SR-1@1"])
        _swc(
            tmp_path,
            "Brake",
            satisfies=["SR-1@1"],
            ports=[{"name": "pTemp", "direction": "require", "interface": "I", "satisfies": ["SR-1@1"]}],
            runnables=[{"name": "Run", "satisfies": ["SR-1@1"]}],
        )
        _write(
            element_path(tmp_path, "interfaces", "ISr"),
            {"name": "ISr", "kind": "sender_receiver", "data_elements": _ONE_DATA_ELEMENT, "satisfies": ["SR-1@1"]},
        )
        _hardware(tmp_path, "Cpu", "processor", satisfies=["SR-1@1"])
        _write(
            element_path(tmp_path, "tasks", "T"),
            {"name": "T", "period": 0.01, "priority": 1, "partition": "P0", "satisfies": ["SR-1@1"]},
        )
        _write(
            element_path(tmp_path, "compositions", "Vehicle"),
            {"name": "Vehicle", "title": "Vehicle", "satisfies": ["SR-1@1"]},
        )

        paths = {need_id: need["swcraft_path"] for need_id, need in _needs(tmp_path).items()}

        assert paths == {
            "AR-1": "requirements/AR_1.yaml",
            "SWC-Brake": "swc/group/Brake.yaml",
            "PORT-Brake-pTemp": "swc/group/Brake.yaml",
            "RUN-Brake-Run": "swc/group/Brake.yaml",
            "IF-ISr": "interfaces/ISr.yaml",
            "HW-Cpu": "hardware/Cpu.yaml",
            "TASK-T": "tasks/T.yaml",
            "COMP-Vehicle": "compositions/Vehicle.yaml",
        }

    def test_a_suffixed_file_is_named_as_it_is_stored(self, tmp_path):
        """A case-insensitive collision gives the second file a ``~N`` suffix; the need names that file."""
        _incoming(tmp_path, "SR-1")
        _swc(tmp_path, "Brake", ports=[{"name": "p", "direction": "provide", "interface": "I", "satisfies": ["SR-1"]}])
        _write(
            tmp_path / "swc" / "group" / "brake~1.yaml",
            {
                "name": "brake",
                "type": "application",
                "handle_termination_and_restart": "no_support",
                "runnables": [{"name": "Run", "satisfies": ["SR-1"]}],
            },
        )
        _write(
            tmp_path / "interfaces" / "isr~1.yaml",
            {"name": "isr", "kind": "sender_receiver", "data_elements": _ONE_DATA_ELEMENT, "satisfies": ["SR-1"]},
        )

        paths = {need_id: need["swcraft_path"] for need_id, need in _needs(tmp_path).items()}

        assert paths == {
            "PORT-Brake-p": "swc/group/Brake.yaml",
            "RUN-brake-Run": "swc/group/brake~1.yaml",
            "IF-isr": "interfaces/isr~1.yaml",
        }

    def test_a_derived_from_element_target_names_its_file(self, tmp_path):
        _req_file(tmp_path, "AR_1", "AR-1", derived_from=["swc:Brake"])
        _swc(tmp_path, "Brake")

        assert _needs(tmp_path)["SWC-Brake"]["swcraft_path"] == "swc/group/Brake.yaml"

    @pytest.mark.parametrize("relative", [False, True])
    def test_no_value_is_absolute_whichever_way_the_model_root_is_given(self, tmp_path, monkeypatch, relative):
        model = tmp_path / "project" / "model"
        _incoming(model, "SR-1")
        _req_file(model, "AR_1", "AR-1", derived_from=["SR-1@1"])
        _swc(model, "Brake", ports=[{"name": "p", "direction": "provide", "interface": "I", "satisfies": ["SR-1"]}])
        if relative:
            monkeypatch.chdir(tmp_path)
        root = "project/model" if relative else str(model)

        text = build_needs_json(root, "Demo")
        document = json.loads(text)
        needs = document["versions"][document["current_version"]]["needs"]

        assert str(tmp_path) not in text and tmp_path.as_posix() not in text
        for need_id, need in needs.items():
            path = need["swcraft_path"]
            assert not pathlib.PurePosixPath(path).is_absolute() and not pathlib.PureWindowsPath(path).is_absolute()
            assert "\\" not in path and not path.startswith(("project", "model")), (need_id, path)
            assert (model / path).is_file(), (need_id, path)


class TestTheBoundary:
    def test_the_module_imports_trace_and_never_validator(self):
        imported: list[str] = []
        for node in ast.walk(ast.parse(_MODULE.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)

        assert "swcraft_tools.trace" in imported
        assert not any("validator" in name for name in imported), imported
        assert "swcraft_tools.schema" not in imported


# ---------------------------------------------------------------------------
# The refusals: nothing is written
# ---------------------------------------------------------------------------


_DYNAMIC = (
    "holds [[ once its backslashes, whitespace and non-printing characters are removed, "
    "which Sphinx-Needs reads as a dynamic function in a need's title or content"
)

_SEPARATOR = (
    "an id holding '.', which Sphinx-Needs reads as an id and a part under every configuration, so the link "
    "would name another need or none; SWCraft does not support such an id in a published link"
)


class TestTwoNeedsUnderOneId:
    def test_an_architecture_requirement_beside_a_component_of_that_id(self, tmp_path):
        _req_file(tmp_path, "A", "SWC-Brake")
        _swc(tmp_path, "Brake", satisfies=["SWC-Brake"])

        assert _refusal(tmp_path) == (
            "the id SWC-Brake would be taken by more than one need: requirement SWC-Brake, element SWC-Brake"
        )

    def test_a_member_beside_an_architecture_requirement(self, tmp_path):
        _req_file(tmp_path, "A", "PORT-Brake-p")
        _swc(tmp_path, "Brake", ports=[{"name": "p", "direction": "provide", "interface": "I", "satisfies": ["X"]}])

        assert _refusal(tmp_path) == (
            "the id PORT-Brake-p would be taken by more than one need: requirement PORT-Brake-p, element PORT-Brake-p"
        )

    def test_one_architecture_requirement_id_declared_twice(self, tmp_path):
        """One key of the file cannot hold two needs, so the second is refused rather than dropped."""
        _req_file(tmp_path, "A", "AR-1")
        _req_file(tmp_path, "B", "AR-1")

        assert _refusal(tmp_path) == (
            "the id AR-1 would be taken by more than one need: requirement AR-1, requirement AR-1"
        )

    def test_several_collisions_are_one_refusal_in_id_order(self, tmp_path):
        _req_file(tmp_path, "A", "SWC-Brake")
        _req_file(tmp_path, "C", "HW-Core0")
        _swc(tmp_path, "Brake", satisfies=["X"])
        _hardware(tmp_path, "Core0", "core", threshold=0.7, satisfies=["X"])

        assert _refusal(tmp_path) == (
            "the id HW-Core0 would be taken by more than one need: requirement HW-Core0, element HW-Core0; "
            "the id SWC-Brake would be taken by more than one need: requirement SWC-Brake, element SWC-Brake"
        )

    def test_an_unlinked_element_cannot_collide(self, tmp_path):
        _req_file(tmp_path, "A", "SWC-Brake")
        _swc(tmp_path, "Brake")

        assert sorted(_needs(tmp_path)) == ["SWC-Brake"]


class TestANeedUnderAnIncomingId:
    def test_an_element_whose_id_is_a_held_incoming_requirements(self, tmp_path):
        _incoming(tmp_path, "SWC-Brake")
        _swc(tmp_path, "Brake", satisfies=["SWC-Brake@1"])

        assert _refusal(tmp_path) == (
            "element SWC-Brake cannot be published: its id SWC-Brake is an incoming requirement's id, and the "
            "requirements team's own need would replace it in their build"
        )

    def test_an_architecture_requirement_whose_id_a_stub_holds(self, tmp_path):
        _incoming(tmp_path, "SR-1", baseline=None, stub=True)
        _req_file(tmp_path, "AR", "SR-1")

        assert _refusal(tmp_path) == (
            "requirement SR-1 cannot be published: its id SR-1 is an incoming requirement's id, and the "
            "requirements team's own need would replace it in their build"
        )


class TestADynamicFunctionIsNeverPublished:
    @pytest.mark.parametrize("field", ["title", "description"])
    def test_a_requirement_title_or_description_holding_one(self, tmp_path, field):
        _req_file(tmp_path, "A", "AR-1", **{field: "See [[copy('id')]]"})

        assert _refusal(tmp_path) == f"requirement AR-1 cannot be published: its {field} {_DYNAMIC}"

    def test_an_element_or_member_name_or_description_holding_one(self, tmp_path):
        _write(
            element_path(tmp_path, "hardware", "Core0"),
            {"name": "Core[[0]]", "kind": "core", "threshold": 0.7, "satisfies": ["X"]},
        )
        _swc(
            tmp_path,
            "Brake",
            description="[[release]]",
            satisfies=["X"],
            runnables=[{"name": "Run", "description": "a [[b]] c", "satisfies": ["X"]}],
        )

        assert _refusal(tmp_path) == (
            f"element SWC-Brake cannot be published: its description {_DYNAMIC}; "
            f"element RUN-Brake-Run cannot be published: its description {_DYNAMIC}; "
            f"element HW-Core[[0]] cannot be published: its name {_DYNAMIC}"
        )

    @pytest.mark.parametrize(
        "description",
        [
            "see `link <https://example.com/\\[\\[copy('id')]]>`_ here",
            "see `link <https://example.com/[ [copy('id')]]>`_ here",
            "see [\\\n[copy('id')]] here",
        ],
    )
    def test_one_that_removing_backslashes_or_whitespace_would_form(self, tmp_path, description):
        _req_file(tmp_path, "A", "AR-1", description=description)

        assert _refusal(tmp_path) == f"requirement AR-1 cannot be published: its description {_DYNAMIC}"

    @pytest.mark.parametrize("hidden", ["\x00", "​", "﻿", "­", "", "͸"])
    def test_one_split_by_a_non_printing_character(self, tmp_path, hidden):
        _req_file(tmp_path, "A", "AR-1", title=f"see [{hidden}[copy('id')]]")

        assert _refusal(tmp_path) == f"requirement AR-1 cannot be published: its title {_DYNAMIC}"

    @pytest.mark.parametrize("description", ["see [x] and [y] here", "a [​x] and [y­] here"])
    def test_brackets_that_never_form_one_are_published(self, tmp_path, description):
        _req_file(tmp_path, "A", "AR-1", description=description)

        assert _needs(tmp_path)["AR-1"]["content"] == description

    def test_an_unpublished_element_or_incoming_requirement_holding_one_is_not_refused(self, tmp_path):
        _swc(tmp_path, "Brake", description="[[release]]")
        _incoming(tmp_path, "SR-1", title="[[copy('id')]]")

        assert _needs(tmp_path) == {}


class TestALinkToAnIdHoldingADot:
    """A link to an id holding ``.`` is refused; ``[`` is published.

    Sphinx-Needs 9.2.0 splits every link value at its first ``.`` (``NeedLink.parse_address``) under every
    configuration."""

    def test_a_satisfies_naming_a_dotted_id(self, tmp_path):
        _incoming(tmp_path, "SR.3.1")
        _swc(
            tmp_path,
            "Brake",
            ports=[{"name": "pTemp", "direction": "require", "interface": "I", "satisfies": ["SR.3.1@1"]}],
        )

        assert _refusal(tmp_path) == (
            f"element PORT-Brake-pTemp cannot be published: its satisfies names SR.3.1, {_SEPARATOR}"
        )

    def test_a_derived_from_naming_a_bracketed_id_is_published_whole(self, tmp_path):
        """The guide's block sets ``"parse_conditions": False``, so the team's build reads ``AB[1]`` whole."""
        _incoming(tmp_path, "AB[1]")
        _req_file(tmp_path, "AR_1", "AR-1", derived_from=["AB[1]@1"])

        assert _needs(tmp_path)["AR-1"]["derived_from"] == ["AB[1]"]

    def test_a_satisfies_naming_a_bracketed_id_and_one_whose_own_id_holds_one(self, tmp_path):
        _incoming(tmp_path, "SYS_REQ[12]")
        _req_file(tmp_path, "AR_1", "AR[1]")
        _swc(tmp_path, "Brake", satisfies=["SYS_REQ[12]@1", "AR[1]@1", "SYS_REQ[[9]]"])

        needs = _needs(tmp_path)
        assert needs["SWC-Brake"]["satisfies"] == ["SYS_REQ[12]", "AR[1]"]
        assert needs["SWC-Brake"]["satisfies_unconfirmed"] == ["SYS_REQ[[9]]"]
        assert "AR[1]" in needs

    def test_an_id_holding_both_is_refused_for_its_dot(self, tmp_path):
        _incoming(tmp_path, "A.B[1]")
        _swc(tmp_path, "Brake", satisfies=["A.B[1]@1"])

        assert _refusal(tmp_path) == f"element SWC-Brake cannot be published: its satisfies names A.B[1], {_SEPARATOR}"

    def test_an_unconfirmed_link_to_an_undeclared_dotted_id_is_refused_too(self, tmp_path):
        _swc(tmp_path, "Brake", satisfies=["GHOST.1"])

        assert _refusal(tmp_path) == f"element SWC-Brake cannot be published: its satisfies names GHOST.1, {_SEPARATOR}"


_UNPLACED = "so its swcraft_path would not name the one file that holds it"


class TestAnElementSeveralFilesDeclare:
    def test_a_component_two_groups_declare_one_of_them_linked(self, tmp_path):
        """``check-model`` reports the duplicate too; the writer cannot pick one of the two files."""
        _incoming(tmp_path, "SR-1")
        _write(
            element_path(tmp_path, "swc", "Brake", group="a"),
            {"name": "Brake", "type": "application", "handle_termination_and_restart": "no_support"},
        )
        _write(
            element_path(tmp_path, "swc", "Brake", group="b"),
            {
                "name": "Brake",
                "type": "application",
                "handle_termination_and_restart": "no_support",
                "satisfies": ["SR-1@1"],
                "ports": [{"name": "p", "direction": "provide", "interface": "I", "satisfies": ["SR-1@1"]}],
            },
        )

        assert _refusal(tmp_path) == (
            f"element PORT-Brake-p cannot be published: more than one file declares its component Brake, {_UNPLACED}; "
            f"element SWC-Brake cannot be published: more than one file declares it, {_UNPLACED}"
        )

    def test_a_hardware_element_two_files_declare(self, tmp_path):
        _hardware(tmp_path, "Cpu", "processor", satisfies=["X"])
        _write(tmp_path / "hardware" / "cpu_copy.yaml", {"name": "Cpu", "kind": "core", "threshold": 0.7})

        assert _refusal(tmp_path) == (
            f"element HW-Cpu cannot be published: more than one file declares it, {_UNPLACED}"
        )

    def test_both_copies_linked_is_the_collision_alone(self, tmp_path):
        """Two needs under one id already refuse it; the clause would repeat that refusal."""
        _hardware(tmp_path, "Cpu", "processor", satisfies=["X"])
        _write(
            tmp_path / "hardware" / "cpu_copy.yaml",
            {"name": "Cpu", "kind": "core", "threshold": 0.7, "satisfies": ["X"]},
        )

        assert _refusal(tmp_path) == (
            "the id HW-Cpu would be taken by more than one need: element HW-Cpu, element HW-Cpu"
        )

    def test_an_unpublished_element_two_files_declare_is_not_refused(self, tmp_path):
        _hardware(tmp_path, "Cpu", "processor")
        _write(tmp_path / "hardware" / "cpu_copy.yaml", {"name": "Cpu", "kind": "core", "threshold": 0.7})

        assert _needs(tmp_path) == {}


class TestSeveralDeliveries:
    def test_two_deliveries(self, tmp_path):
        for index in range(3):
            _incoming(tmp_path, f"SR-{index}", baseline="2.3")
        _incoming(tmp_path, "SR-9", baseline="2.2")

        assert _refusal(tmp_path) == (
            "the imported requirements come from more than one delivery (2.3: 3 requirements, 2.2: 1); "
            "import the current delivery, then generate"
        )

    def test_a_delivery_beside_none(self, tmp_path):
        _incoming(tmp_path, "SR-1", baseline="2.3")
        _incoming(tmp_path, "SR-2", baseline=None)

        assert _refusal(tmp_path) == (
            "the imported requirements come from more than one delivery (2.3: 1 requirement, none: 1); "
            "import the current delivery, then generate"
        )


class TestOneRefusalForTheWholeModel:
    def test_every_clause_is_joined_in_reading_order_then_collisions_then_the_delivery(self, tmp_path):
        _incoming(tmp_path, "SR.1", baseline="2.3")
        _incoming(tmp_path, "SR-2", baseline="2.2")
        _req_file(tmp_path, "AR_1", "AR-1", title="[[x]]", derived_from=["SR.1@1"])
        _req_file(tmp_path, "AR_2", "SWC-Brake")
        _swc(tmp_path, "Brake", satisfies=["SR-2@1"])

        assert _refusal(tmp_path) == (
            f"requirement AR-1 cannot be published: its title {_DYNAMIC}; "
            f"requirement AR-1 cannot be published: its derived_from names SR.1, {_SEPARATOR}; "
            "the id SWC-Brake would be taken by more than one need: requirement SWC-Brake, element SWC-Brake; "
            "the imported requirements come from more than one delivery (2.2: 1 requirement, 2.3: 1); "
            "import the current delivery, then generate"
        )

    def test_the_several_files_clause_comes_after_the_collisions_and_before_the_delivery(self, tmp_path):
        _incoming(tmp_path, "SR-1", baseline="2.3")
        _incoming(tmp_path, "SR-2", baseline="2.2")
        _req_file(tmp_path, "AR_1", "SWC-Brake")
        _swc(tmp_path, "Brake", satisfies=["SR-1@1"])
        _hardware(tmp_path, "Cpu", "processor", satisfies=["SR-1@1"])
        _write(tmp_path / "hardware" / "cpu_copy.yaml", {"name": "Cpu", "kind": "core", "threshold": 0.7})

        assert _refusal(tmp_path) == (
            "the id SWC-Brake would be taken by more than one need: requirement SWC-Brake, element SWC-Brake; "
            f"element HW-Cpu cannot be published: more than one file declares it, {_UNPLACED}; "
            "the imported requirements come from more than one delivery (2.2: 1 requirement, 2.3: 1); "
            "import the current delivery, then generate"
        )

    def test_an_incoming_id_refusal_is_not_repeated_by_the_several_files_clause(self, tmp_path):
        _incoming(tmp_path, "HW-Cpu")
        _hardware(tmp_path, "Cpu", "processor", satisfies=["HW-Cpu@1"])
        _write(tmp_path / "hardware" / "cpu_copy.yaml", {"name": "Cpu", "kind": "core", "threshold": 0.7})

        assert _refusal(tmp_path) == (
            "element HW-Cpu cannot be published: its id HW-Cpu is an incoming requirement's id, and the "
            "requirements team's own need would replace it in their build"
        )

    def test_a_line_break_in_a_link_target_or_a_delivery_is_shown_escaped_so_the_refusal_is_one_line(self, tmp_path):
        _incoming(tmp_path, "SR-1", baseline="2.3\n")
        _incoming(tmp_path, "SR-2", baseline="2.2")
        _swc(tmp_path, "Brake", satisfies=["SR.1\nX"])

        message = _refusal(tmp_path)

        assert "\n" not in message and "\r" not in message
        assert message == (
            f"element SWC-Brake cannot be published: its satisfies names SR.1\\nX, {_SEPARATOR}; "
            "the imported requirements come from more than one delivery (2.2: 1 requirement, 2.3\\n: 1); "
            "import the current delivery, then generate"
        )

    def test_the_refusal_names_no_path(self, tmp_path):
        _req_file(tmp_path, "A", "SWC-Brake")
        _swc(tmp_path, "Brake", satisfies=["SWC-Brake"])

        message = _refusal(tmp_path)

        assert str(tmp_path) not in message
        assert tmp_path.as_posix() not in message
        assert ".yaml" not in message


def _two_needs_under_one_id(model) -> None:
    _req_file(model, "A", "SWC-Brake")
    _swc(model, "Brake", satisfies=["SWC-Brake"])


def _a_need_under_an_incoming_id(model) -> None:
    _incoming(model, "SWC-Brake")
    _swc(model, "Brake", satisfies=["SWC-Brake@1"])


def _a_dynamic_function(model) -> None:
    _req_file(model, "A", "AR-1", title="[[copy('id')]]")


def _a_link_to_a_dotted_id(model) -> None:
    _incoming(model, "SR.1")
    _swc(model, "Brake", satisfies=["SR.1@1"])


def _an_element_two_files_declare(model) -> None:
    _hardware(model, "Cpu", "processor", satisfies=["X"])
    _write(pathlib.Path(model) / "hardware" / "cpu_copy.yaml", {"name": "Cpu", "kind": "core", "threshold": 0.7})


def _two_deliveries(model) -> None:
    _incoming(model, "SR-1", baseline="2.3")
    _incoming(model, "SR-2", baseline="2.2")


_EVERY_CAUSE = pytest.mark.parametrize(
    "refused_model",
    [
        _two_needs_under_one_id,
        _a_need_under_an_incoming_id,
        _a_dynamic_function,
        _a_link_to_a_dotted_id,
        _an_element_two_files_declare,
        _two_deliveries,
    ],
    ids=lambda cause: cause.__name__.lstrip("_"),
)


class TestARefusalWritesNothing:
    """Each of the six causes, through ``generate_needs``: no file, no folder, and a previous file removed."""

    @_EVERY_CAUSE
    def test_a_refused_generate_creates_no_file_and_no_folder(self, tmp_path, refused_model):
        model = tmp_path / "model"
        refused_model(model)
        output = tmp_path / "dist" / "needs.json"

        with pytest.raises(NeedsExportRefused):
            generate_needs(str(model), str(output), "Demo")

        assert not (tmp_path / "dist").exists()

    @_EVERY_CAUSE
    def test_a_refusal_over_an_existing_file_removes_it(self, tmp_path, refused_model):
        """A file the model no longer produces is not left to be handed over."""
        model = tmp_path / "model"
        _req_file(model, "AR_1", "AR-1")
        output = tmp_path / "dist" / "needs.json"
        generate_needs(str(model), str(output), "Demo")
        assert output.is_file()
        refused_model(model)

        with pytest.raises(NeedsExportRefused) as refused:
            generate_needs(str(model), str(output), "Demo")

        assert not output.exists()
        assert sorted(path.name for path in output.parent.iterdir()) == []
        assert "could not be removed" not in str(refused.value)

    def test_a_model_that_cannot_be_read_removes_the_previous_file_too(self, tmp_path):
        """No text is built, so the earlier file no longer describes the model."""
        model = tmp_path / "model"
        _req_file(model, "AR_1", "AR-1")
        output = tmp_path / "dist" / "needs.json"
        generate_needs(str(model), str(output), "Demo")
        (model / "requirements" / "AR_1.yaml").write_text("id: [unclosed\n", encoding="utf-8", newline="\n")

        with pytest.raises(Exception) as raised:
            generate_needs(str(model), str(output), "Demo")

        assert not isinstance(raised.value, NeedsExportRefused)
        assert not output.exists()

    def test_a_removal_that_fails_keeps_the_refusal_and_says_the_file_is_still_there(self, tmp_path):
        """A folder at the output's name cannot be unlinked: the clause names the file by its name alone."""
        model = tmp_path / "model"
        _a_link_to_a_dotted_id(model)
        output = tmp_path / "dist" / "needs.json"
        output.mkdir(parents=True)

        with pytest.raises(NeedsExportRefused) as refused:
            generate_needs(str(model), str(output), "Demo")

        text = str(refused.value)
        assert text.startswith("element SWC-Brake cannot be published: its satisfies names SR.1, ")
        assert "; the previous needs.json could not be removed (" in text
        assert text.endswith(") and does not describe the model: do not hand it over")
        assert str(tmp_path) not in text
        assert output.is_dir()


class TestAFailedWriteKeepsThePreviousFile:
    """The text goes to a temporary sibling, closed before ``os.replace`` puts it over the file."""

    @staticmethod
    def _previous(tmp_path) -> tuple[pathlib.Path, pathlib.Path, bytes]:
        model = tmp_path / "model"
        _req_file(model, "AR_1", "AR-1")
        output = tmp_path / "dist" / "needs.json"
        generate_needs(str(model), str(output), "Demo")
        _req_file(model, "AR_2", "AR-2")
        return model, output, output.read_bytes()

    @pytest.mark.parametrize("failing", ["write", "replace"])
    def test_a_failing_write_or_replace_leaves_the_previous_bytes_and_no_temporary_file(
        self, tmp_path, monkeypatch, failing
    ):
        model, output, before = self._previous(tmp_path)

        def _refuse(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        if failing == "replace":
            monkeypatch.setattr("swcraft_tools.writer.needs_export.os.replace", _refuse)
        else:
            original_open = open

            class _FailingHandle:
                def __init__(self, handle):
                    self._handle = handle

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    self._handle.close()

                def close(self):
                    self._handle.close()

                def write(self, text):
                    self._handle.write(text[: len(text) // 2])
                    self._handle.flush()
                    _refuse()

            def _open(path, *args, **kwargs):
                return _FailingHandle(original_open(path, *args, **kwargs))

            # The temporary file is opened by the shared helper (`exclusive_create.open_new`).
            monkeypatch.setattr("swcraft_tools.exclusive_create.open", _open, raising=False)

        with pytest.raises(OSError, match="No space left"):
            generate_needs(str(model), str(output), "Demo")

        assert output.read_bytes() == before
        assert sorted(path.name for path in output.parent.iterdir()) == ["needs.json"]

    def test_a_successful_write_replaces_the_previous_file_and_leaves_no_temporary_file(self, tmp_path):
        model, output, before = self._previous(tmp_path)

        generate_needs(str(model), str(output), "Demo")

        assert output.read_bytes() == _build(model).encode("utf-8") != before
        assert sorted(path.name for path in output.parent.iterdir()) == ["needs.json"]


#: The committed model trees ``needs.json`` is held byte-identical on through the shared spelling:
#: each writes at least one need, so each document carries ``swcraft_path``.
_SHARED_SPELLING_MODELS = (
    "examples/basic/model",
    "packages/tools/tests/fixtures/showcase/model",
    "packages/tools/tests/fixtures/compositions/model",
    "packages/tools/tests/fixtures/diagrams/model",
    "packages/tools/tests/fixtures/diagram_collisions/model",
)


@pytest.mark.parametrize("tree", _SHARED_SPELLING_MODELS)
def test_needs_json_is_byte_identical_through_the_kernels_shared_spelling(
    tree: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``needs.json`` built through ``model_state.stored_storage_names`` equals the collector's own spelling.

    That spelling (``stored_element_files``' entries made relative to the root, POSIX) is put back in
    place for the second build. What it does not reach: a tree not listed here.
    """
    model_root = str(REPO_ROOT / tree)
    shared = build_needs_json(model_root, "Demo")

    def former(self: needs_export._Collector, layer: str, identity: str) -> list[str]:
        root = pathlib.Path(model_root)
        stored = stored_element_files(root, layer, registry=current_registry())
        return [entry.path.relative_to(root).as_posix() for entry in stored.get(identity, ())]

    monkeypatch.setattr(needs_export._Collector, "storage_names", former)
    assert build_needs_json(model_root, "Demo") == shared
    assert '"swcraft_path": ' in shared

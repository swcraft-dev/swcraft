"""``import component`` resolves against the project it would leave.

Most cases take an interface **out of** SWCraft's own export of one component, so a port names a path
the file declares nowhere: a file from elsewhere whose interface the receiving project holds. The
first-tier and file-tier cases keep it (the latter change a member reference). The rules:

* the two resolver tiers: the file's declaration at the exact path, then the target's **declared**
  interface by the path's last segment -- never a target stub by its leaf, never the file's own
  interface of that leaf under another path;
* the gate: a component with a verdict is created with its descriptive part and its resolved
  ``data_type_mappings``, without ``runnables`` and each unresolved set, and ``check-model``
  passes; an unsupported construct and a step-3a defect still refuse the import whole;
* the stub path: a path neither tier answers reuses the target's stub of the same
  ``external_ref`` or becomes a stub of its own, members from the port's com-specs alone, every
  behavioural reference through it a verdict;
* a com-spec value loss on a descriptive port is a line under *Not carried*, the port written;
* a verdict per reference kind (a data element, an operation, a mode group, a mapping set) with its layer and
  element; step 3a run again over the late-admitted names; the mapper's two checks once an interface is known
  (a by-value read of a queued element, a queued receiver's queue length);
* ``--update`` over a component the target holds keeps the project's behaviour, writes the file's descriptive
  part when the validator's predicates find nothing the overlay breaks, and refuses, naming the reference, when
  they do;
* the report block's line per verdict kind, path-free, on the preview, under ``--dry-run`` and in a ``--report``
  file; a minted stub marked in the CREATED list, a reused one in the REUSED list.

What it does **not** reach: real tool exports (the closed suites, ``dev.py test-internal``). A
stub's name is asserted through ``registry.stub_name``, never as a literal, so a change of the
naming form moves no case here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from conftest import write_model_element
from mode_bit_support import read_denied
from project_support import files_of, init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.registry import stub_name
from swcraft_tools.arxml_import.unresolved import COMPONENT_IMPORT_ROLES, UnresolvedReferences, UnresolvedReferrer
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.cli import main
from swcraft_tools.component_exchange import (
    AmbiguousTargetProjectError,
    ComponentExchangeError,
    ComponentImportConflictError,
    LossyComponentImportError,
    export_component,
    import_component,
)
from swcraft_tools.component_exchange.dependencies import MissingReference, target_declared_interface
from swcraft_tools.component_exchange.import_ import ReservedNameError, plan_component_import
from swcraft_tools.component_exchange.report import (
    MISSING_REFERENCES_HEADING,
    MISSING_REFERENCES_HEADING_TAIL,
    MISSING_REFERENCES_KEPT_TAIL,
    MISSING_REFERENCES_UNCHANGED_TAIL,
    STUB_INTERFACE_MARK,
)
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH
from swcraft_tools.model_state import ElementParseError
from swcraft_tools.reader import find_swc_file, read_swc_model
from swcraft_tools.schema import CSInterface, ExternalInterface, SRInterface, SWComponent
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml

# The source project and its export

_U8_BASE = {"name": "u8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"}
_U8 = {"name": "u8", "kind": "value_type", "base": "u8_base"}
_SR = "SENDER-RECEIVER-INTERFACE"


def _sig(*elements: str, queued: frozenset[str] = frozenset()) -> dict:
    return {
        "name": "ISig",
        "kind": "sender_receiver",
        "data_elements": [
            {"name": name, "implementation_type": "u8", **({"sw_impl_policy": "queued"} if name in queued else {})}
            for name in elements
        ],
    }


def _srv(*operations: str) -> dict:
    return {"name": "ISrv", "kind": "client_server", "operations": [{"name": name} for name in operations]}


def _component(read_by: str | None = None) -> dict:
    read: dict = {"port": "rSig", "element": "a"}
    if read_by is not None:
        read["by"] = read_by
    return {
        "name": "Comp",
        "type": "application",
        "ports": [
            {"name": "rSig", "direction": "require", "interface": "ISig"},
            {"name": "pSrv", "direction": "provide", "interface": "ISrv"},
        ],
        "runnables": [
            {"name": "R", "events": [{"type": "timing", "period": 0.01}], "reads": [read]},
            {"name": "S", "events": [{"type": "operation", "port": "pSrv", "operation": "Op"}]},
        ],
        "handle_termination_and_restart": "no_support",
    }


def _seed_types(model_root: Path) -> None:
    write_model_element(model_root, _U8_BASE)
    write_model_element(model_root, _U8)


def _export(tmp_path: Path, *, read_by: str | None = None, wcet: str | None = None) -> str:
    """SWCraft's own export of ``Comp``, as text: ``ISig`` (element ``a``) and ``ISrv`` (operation ``Op``).

    *wcet* budgets runnable ``R`` (``gross``), so the export carries ``Comp_Timing``.
    """
    source = init_project(tmp_path / "source")
    model_root = source / "model"
    _seed_types(model_root)
    write_model_element(model_root, _sig("a"))
    write_model_element(model_root, _srv("Op"))
    swc = model_root / "swc" / "body" / "Comp.yaml"
    swc.parent.mkdir(parents=True, exist_ok=True)
    component = _component(read_by)
    if wcet is not None:
        component["runnables"][0].update(wcet=wcet, wcet_type="gross")
    swc.write_text(yaml.safe_dump(component, sort_keys=False), encoding="utf-8", newline="\n")
    arxml = tmp_path / "Comp.arxml"
    export_component(str(model_root), "Comp", str(arxml))
    return arxml.read_text(encoding="utf-8")


def _without(text: str, tag: str, name: str) -> str:
    """*text* with the ``<tag>`` element whose ``SHORT-NAME`` is *name* removed: the file no longer declares it."""
    pattern = re.compile(rf"\s*<{tag}>\s*<SHORT-NAME>{name}</SHORT-NAME>.*?</{tag}>", re.DOTALL)
    edited, count = pattern.subn("", text, count=1)
    assert count == 1, f"no {tag} {name} in the export"
    return edited


def _without_com_specs(text: str) -> str:
    """*text* with every port's com-spec block removed: the ports state no com-spec at all.

    A file whose behaviour alone makes the reference names exactly the behavioural verdicts;
    :class:`TestInitsAndComSpecsAfterResolution` pins the shape with the com-specs kept.
    """
    edited, count = re.subn(r"\s*<(REQUIRED|PROVIDED)-COM-SPECS>.*?</\1-COM-SPECS>", "", text, flags=re.DOTALL)
    assert count, "no com-spec block in the export"
    return edited


def _save(tmp_path: Path, text: str, name: str = "edited.arxml") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return str(path)


def _export_with_an_illegal_operation_name(tmp_path: Path) -> str:
    """The export with ``ISig`` removed and ``ISrv``'s operation renamed past the identifier rule's length limit."""
    illegal = "O" * (MAX_IDENTIFIER_LENGTH + 2)
    text = _export(tmp_path).replace("<SHORT-NAME>Op</SHORT-NAME>", f"<SHORT-NAME>{illegal}</SHORT-NAME>")
    text = text.replace("/PortInterfaces/ISrv/Op<", f"/PortInterfaces/ISrv/{illegal}<")
    return _save(tmp_path, _without(text, "SENDER-RECEIVER-INTERFACE", "ISig"))


def _target(tmp_path: Path, *interfaces: dict, name: str = "target") -> Path:
    """A fresh project holding the types and *interfaces*; returns its model root."""
    model_root = init_project(tmp_path / name) / "model"
    _seed_types(model_root)
    for interface in interfaces:
        write_model_element(model_root, interface)
    return model_root


def _written_swc(model_root: Path) -> dict:
    return yaml.safe_load((model_root / "swc" / "components" / "Comp.yaml").read_text(encoding="utf-8"))


def _interface_file(model_root: Path, name: str) -> dict:
    return yaml.safe_load((model_root / "interfaces" / f"{name}.yaml").read_text(encoding="utf-8"))


def _withheld(source: str, model_root: Path) -> tuple[list[MissingReference], dict]:
    """Import *source* into *model_root*, asserting the gate withheld the behaviour; the verdicts and the file."""
    planned = plan_component_import(source, str(model_root))
    report = import_component(source, str(model_root))
    assert report.swc_action == "created"
    assert planned.missing_references, "no verdict: the gate had nothing to withhold"
    assert report.missing_references == [record.line() for record in planned.missing_references]
    written = _written_swc(model_root)
    assert written.get("runnables", []) == []
    unresolved = {m.missing_name for m in planned.missing_references if m.reference_kind == "mapping_set"}
    assert not unresolved & set(written.get("data_type_mappings", []))
    assert written["handle_termination_and_restart"] == "no_support"
    return list(planned.missing_references), written


def _link_and_check(model_root: Path) -> None:
    """A requirement ``Comp`` satisfies, then ``check-model``'s composer: the result must be consistent."""
    requirement = {
        "id": "REQ_COMP",
        "title": "Comp",
        "description": "The component.",
        "version": 1,
        "source": "architecture",
    }
    (model_root / "requirements" / "REQ_COMP.yaml").write_text(
        yaml.safe_dump(requirement, sort_keys=False), encoding="utf-8", newline="\n"
    )
    component = Path(find_swc_file(str(model_root), "Comp"))
    document = yaml.safe_load(component.read_text(encoding="utf-8"))
    document["satisfies"] = [*document.get("satisfies", []), "REQ_COMP@1"]
    component.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    result = check_cross_layer_consistency(str(model_root))
    assert result.consistent, result.issues


def _refusal(source: str, model_root: Path) -> LossyComponentImportError:
    before = files_of(model_root)
    with pytest.raises(LossyComponentImportError) as caught:
        import_component(source, str(model_root))
    assert files_of(model_root) == before, "a refused import wrote something"
    return caught.value


# The policy: the role filter, withdraw, reuse


class TestTheComponentPolicy:
    """``UnresolvedReferences.for_component_import``: one role, resolved late, and the two late answers."""

    def _policy(self, tmp_path: Path) -> UnresolvedReferences:
        text = _without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig")
        return UnresolvedReferences.for_component_import(read_arxml(_save(tmp_path, text)))

    @staticmethod
    def _port(field: str = "swc.port.interface") -> UnresolvedReferrer:
        return UnresolvedReferrer(
            path="/ComponentTypes/Comp/rSig", tag="R-PORT-PROTOTYPE", attribute="REQUIRED-INTERFACE-TREF", field=field
        )

    def test_the_filter_admits_a_port_interface_alone(self, tmp_path: Path) -> None:
        policy = self._policy(tmp_path)

        assert COMPONENT_IMPORT_ROLES == frozenset({"swc.port.interface"})
        assert policy.resolves_late
        assert policy.keep("/PortInterfaces/ISig", self._port()) == "/PortInterfaces/ISig"
        assert policy.keep("/PortInterfaces/ISig", self._port("swc.data_type_mappings")) is None
        assert policy.answer("/Elsewhere/Maps", role="swc.data_type_mappings") is None
        assert policy.answer("/Elsewhere/Maps") is None

    def test_an_unfiltered_policy_answers_every_role(self, tmp_path: Path) -> None:
        """``import model``'s policy is unchanged: no filter, not late."""
        text = _without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig")
        policy = UnresolvedReferences(read_arxml(_save(tmp_path, text)))

        assert not policy.resolves_late
        assert policy.answer("/Elsewhere/Maps") == "/Elsewhere/Maps"
        assert policy.keep("/PortInterfaces/ISig", self._port("swc.data_type_mappings")) == "/PortInterfaces/ISig"

    def test_withdraw_takes_the_record_out_of_the_references(self, tmp_path: Path) -> None:
        policy = self._policy(tmp_path)
        policy.keep("/PortInterfaces/ISig", self._port())
        policy.commit("/ComponentTypes/Comp/rSig")
        assert [record.target for record in policy.references] == ["/PortInterfaces/ISig"]

        withdrawn = policy.withdraw("/PortInterfaces/ISig")

        assert withdrawn is not None and withdrawn.target == "/PortInterfaces/ISig"
        assert policy.references == ()
        assert policy.withdrawn == frozenset({"/PortInterfaces/ISig"})
        assert policy.withdraw("/PortInterfaces/ISig") is None  # idempotent

    def _elsewhere_policy(self, tmp_path: Path) -> UnresolvedReferences:
        """The policy over a file whose ``rSig`` names ``/Elsewhere/ISig``, a path the stub rule admits."""
        policy = UnresolvedReferences.for_component_import(
            read_arxml(_save(tmp_path, _elsewhere(_export(tmp_path), "ISig")))
        )
        policy.keep("/Elsewhere/ISig", self._port())
        policy.commit("/ComponentTypes/Comp/rSig")
        return policy

    def test_reuse_takes_no_rank_and_mints_nothing(self, tmp_path: Path) -> None:
        from swcraft_tools.arxml_import.registry import PathRegistry

        policy = self._elsewhere_policy(tmp_path)
        policy.stage_member(
            "/Elsewhere/ISig",
            kind="data_elements",
            member_ref="/Elsewhere/ISig/a",
            port_path="/ComponentTypes/Comp/rSig",
        )

        assert policy.occurrence_dests("/Elsewhere/ISig") == frozenset({"SENDER-RECEIVER-INTERFACE"})
        stub = policy.reuse(
            "/Elsewhere/ISig", ExternalInterface.model_validate(_target_stub("ISig", "/Elsewhere/ISig"))
        )
        minted = policy.mint_stubs(PathRegistry(), interface_names=(), type_names=())

        assert stub is not None and stub.name == "ISig"
        assert [reused.name for reused in policy.reused] == ["ISig"]
        assert minted.interfaces == ()
        assert policy.references == ()
        # The reused stub's members are an earlier import's inference: nothing is listed for this one.
        assert policy.listed_member("/Elsewhere/ISig", "data_elements", "/Elsewhere/ISig/a") is None

    def test_reuse_declines_a_stub_of_another_dest(self, tmp_path: Path) -> None:
        """The stub rule's safeguards hold on the reuse path: a stub of another kind is never reused."""
        policy = self._elsewhere_policy(tmp_path)

        other_kind = _target_stub("ISig", "/Elsewhere/ISig", dest="CLIENT-SERVER-INTERFACE")
        assert policy.reuse("/Elsewhere/ISig", ExternalInterface.model_validate(other_kind)) is None
        assert policy.reused == ()
        assert [record.target for record in policy.references] == ["/Elsewhere/ISig"]

    def test_reuse_declines_a_path_inside_a_generated_root_package(self, tmp_path: Path) -> None:
        policy = self._policy(tmp_path)
        policy.keep("/PortInterfaces/ISig", self._port())
        policy.commit("/ComponentTypes/Comp/rSig")

        stub = ExternalInterface.model_validate(_target_stub("ISig", "/PortInterfaces/ISig"))
        assert policy.reuse("/PortInterfaces/ISig", stub) is None


# The two tiers


class TestTheSecondTierIsTheTargetsDeclaredInterface:
    def test_a_declared_interface_answers_by_the_last_segment(self) -> None:
        declared = SRInterface.model_validate(_sig("a"))

        assert target_declared_interface("/Elsewhere/Deep/ISig", {"ISig": declared}, {_SR}) is declared
        assert target_declared_interface("/Elsewhere/ISrv", {"ISig": declared}, {_SR}) is None

    @pytest.mark.parametrize(
        "dests",
        [
            {"CLIENT-SERVER-INTERFACE"},
            {"MODE-SWITCH-INTERFACE"},
            {"DATA-INTERFACE"},
            {"PORT-INTERFACE"},
            {_SR, "CLIENT-SERVER-INTERFACE"},
            {""},
            set(),
        ],
    )
    def test_a_reference_of_another_or_no_kind_is_never_answered(self, dests: set[str]) -> None:
        """A declared interface of another kind is never the answer; an abstract or absent ``DEST`` binds nothing."""
        declared = SRInterface.model_validate(_sig("a"))

        assert target_declared_interface("/Elsewhere/ISig", {"ISig": declared}, dests) is None

    def test_a_target_stub_is_never_an_answer_by_its_leaf(self) -> None:
        stub = ExternalInterface(
            name="ISig",
            kind="external_interface",
            external_ref="/Other/ISig",
            external_dest="SENDER-RECEIVER-INTERFACE",
        )

        assert target_declared_interface("/PortInterfaces/ISig", {"ISig": stub}, {_SR}) is None


class TestAComponentFileAnswersItself:
    """The first tier: a complete component into an empty project, no verdict."""

    def test_a_complete_component_into_an_empty_project_is_created(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _export(tmp_path))
        target = init_project(tmp_path / "empty") / "model"

        report = import_component(source, str(target))

        assert report.swc_action == "created"
        assert sorted(report.created_interfaces) == ["ISig", "ISrv"]
        written = _written_swc(target)
        assert written["runnables"][0]["reads"] == [{"port": "rSig", "element": "a"}]


class TestTheTargetAnswersAPortTheFileDoesNotDeclare:
    """The second tier, per reference kind the file makes through such a port."""

    def test_a_read_is_answered_by_the_targets_declared_interface(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig"))
        target = _target(tmp_path, _sig("a"))

        report = import_component(source, str(target))

        assert report.swc_action == "created"
        assert report.created_interfaces == ["ISrv"]
        written = _written_swc(target)
        assert {"name": "rSig", "direction": "require", "interface": "ISig"} in written["ports"]
        assert written["runnables"][0]["reads"] == [{"port": "rSig", "element": "a"}]

    def test_an_operation_event_is_answered_by_the_targets_declared_interface(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _without(_export(tmp_path), "CLIENT-SERVER-INTERFACE", "ISrv"))
        target = _target(tmp_path, _srv("Op"))

        report = import_component(source, str(target))

        assert report.swc_action == "created"
        assert report.created_interfaces == ["ISig"]
        written = _written_swc(target)
        assert {"name": "pSrv", "direction": "provide", "interface": "ISrv"} in written["ports"]
        assert written["runnables"][1]["events"] == [{"type": "operation", "port": "pSrv", "operation": "Op"}]

    def test_a_mapping_set_is_answered_by_the_targets_set(self, tmp_path: Path) -> None:
        text = _export(tmp_path).replace(
            "<HANDLE-TERMINATION-AND-RESTART>",
            '<DATA-TYPE-MAPPING-REFS><DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">/Elsewhere/Maps'
            "</DATA-TYPE-MAPPING-REF></DATA-TYPE-MAPPING-REFS><HANDLE-TERMINATION-AND-RESTART>",
            1,
        )
        target = _target(tmp_path)
        write_model_element(target, {"name": "Maps", "maps": []}, layer="mapping_sets")

        import_component(_save(tmp_path, text), str(target))

        assert _written_swc(target)["data_type_mappings"] == ["Maps"]


class TestAReferenceNoTierAnswersIsAVerdict:
    """One :class:`MissingReference` per reference, with its layer, element and kind."""

    def test_a_data_element_the_targets_interface_lacks(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _without_com_specs(_without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig")))
        target = _target(tmp_path, _sig("other"))

        missing, written = _withheld(source, target)

        assert missing == [
            MissingReference(
                referrer_layer="swc",
                referrer_name="Comp",
                referrer="runnable 'R' read through port 'rSig'",
                missing_member="a",
                reference_kind="data_element",
                missing_layer="interfaces",
                missing_name="ISig",
            )
        ]
        assert "/" not in missing[0].line(), "a verdict line names no path"
        assert {"name": "rSig", "direction": "require", "interface": "ISig"} in written["ports"]

    def test_an_operation_the_targets_interface_lacks(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _without_com_specs(_without(_export(tmp_path), "CLIENT-SERVER-INTERFACE", "ISrv")))
        target = _target(tmp_path, _srv("Other"))

        missing, _written = _withheld(source, target)

        assert [(m.reference_kind, m.missing_member, m.missing_name, m.referrer) for m in missing] == [
            ("operation", "Op", "ISrv", "runnable 'S' operation event through port 'pSrv'")
        ]

    def test_a_target_interface_of_the_same_leaf_and_the_other_kind_is_never_bound(self, tmp_path: Path) -> None:
        """The reference's ``DEST`` names a sender-receiver interface: the target's client-server one is not it.

        Guards a binding by the leaf alone, which bound the port to the wrong element.
        """
        source = _save(tmp_path, _without_com_specs(_elsewhere(_export(tmp_path), "ISig")))
        target = _target(tmp_path, {**_srv("a"), "name": "ISig"})

        missing, written = _withheld(source, target)

        stub = stub_name("ISig", 0, {"ISig", "ISrv"})[0]
        assert stub != "ISig"
        assert {"name": "rSig", "direction": "require", "interface": stub} in written["ports"]
        assert [(m.reference_kind, m.missing_member, m.missing_name, m.missing_is_stub) for m in missing] == [
            ("data_element", "a", stub, True)
        ]
        assert _interface_file(target, "ISig")["kind"] == "client_server"
        _link_and_check(target)

    def test_a_target_interface_of_the_other_kind_under_a_root_package_path_refuses_the_port(
        self, tmp_path: Path
    ) -> None:
        """The stub rule stubs no path inside a generated root package, so the port keeps its refusal."""
        source = _save(tmp_path, _without_com_specs(_without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig")))

        refusal = _refusal(source, _target(tmp_path, {**_srv("a"), "name": "ISig"}))

        assert any("unresolved reference /PortInterfaces/ISig" in line for line in refusal.skip_lines)

    def test_a_mode_switch_reference_is_never_bound_to_a_declared_interface(self, tmp_path: Path) -> None:
        """A ``MODE-SWITCH-INTERFACE`` reference goes to a mode-switch stub, never a target interface of its leaf.

        Guards a binding by the leaf that wrote the disabled mode through a sender-receiver port.
        """
        target = _target(tmp_path, {**_sig("a"), "name": "IMode"})

        missing, written = _withheld(_save(tmp_path, _with_disabled_mode(_export(tmp_path))), target)

        stub = stub_name("IMode", 0, {"ISig", "ISrv", "IMode"})[0]
        assert stub != "IMode"
        assert {"name": "rMode", "direction": "require", "interface": stub} in written["ports"]
        assert _interface_file(target, stub)["external_dest"] == "MODE-SWITCH-INTERFACE"
        assert [(m.reference_kind, m.missing_member, m.missing_name) for m in missing] == [("mode_group", "mg", stub)]
        _link_and_check(target)

    def test_a_mode_group_through_a_port_nothing_answers(self, tmp_path: Path) -> None:
        text = _export(tmp_path)
        text = text.replace(
            "</PORTS>",
            "<R-PORT-PROTOTYPE><SHORT-NAME>rMode</SHORT-NAME>"
            '<REQUIRED-INTERFACE-TREF DEST="MODE-SWITCH-INTERFACE">/Elsewhere/IMode</REQUIRED-INTERFACE-TREF>'
            "</R-PORT-PROTOTYPE></PORTS>",
            1,
        )
        text = text.replace(
            "<SHORT-NAME>TMT_R</SHORT-NAME>",
            "<SHORT-NAME>TMT_R</SHORT-NAME><DISABLED-MODE-IREFS><DISABLED-MODE-IREF>"
            '<CONTEXT-PORT-REF DEST="R-PORT-PROTOTYPE">/ComponentTypes/Comp/rMode</CONTEXT-PORT-REF>'
            '<CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF DEST="MODE-DECLARATION-GROUP-PROTOTYPE">/Elsewhere/IMode/mg'
            "</CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF>"
            '<TARGET-MODE-DECLARATION-REF DEST="MODE-DECLARATION">/Elsewhere/Modes/OFF</TARGET-MODE-DECLARATION-REF>'
            "</DISABLED-MODE-IREF></DISABLED-MODE-IREFS>",
            1,
        )
        missing, written = _withheld(_save(tmp_path, text), _target(tmp_path))

        stub = stub_name("IMode", 0, {"ISig", "ISrv"})[0]
        assert [(m.reference_kind, m.missing_member, m.missing_name) for m in missing] == [("mode_group", "mg", stub)]
        assert {"name": "rMode", "direction": "require", "interface": stub} in written["ports"]

    def test_a_mapping_set_neither_declares(self, tmp_path: Path) -> None:
        text = _export(tmp_path).replace(
            "<HANDLE-TERMINATION-AND-RESTART>",
            '<DATA-TYPE-MAPPING-REFS><DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">/Elsewhere/Maps'
            "</DATA-TYPE-MAPPING-REF></DATA-TYPE-MAPPING-REFS><HANDLE-TERMINATION-AND-RESTART>",
            1,
        )
        missing, _written = _withheld(_save(tmp_path, text), _target(tmp_path))

        assert missing == [
            MissingReference(
                referrer_layer="swc",
                referrer_name="Comp",
                referrer="internal behaviour",
                missing_member="",
                reference_kind="mapping_set",
                missing_layer="mapping_sets",
                missing_name="Maps",
            )
        ]


class TestALeafNeverMergesTwoPaths:
    """The exact-path rule and the stub rule, from the target side."""

    def test_the_files_own_interface_of_the_leaf_under_another_path_is_not_an_answer(self, tmp_path: Path) -> None:
        """``/A/IFoo`` in the file never answers ``/B/IFoo``: the port goes to a stub of its own."""
        text = _without_com_specs(_export(tmp_path)).replace(
            '<REQUIRED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/PortInterfaces/ISig<',
            '<REQUIRED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Elsewhere/ISig<',
        )
        target = _target(tmp_path)

        missing, written = _withheld(_save(tmp_path, text), target)

        stub = stub_name("ISig", 0, {"ISig", "ISrv"})[0]
        assert stub != "ISig"
        assert {"name": "rSig", "direction": "require", "interface": stub} in written["ports"]
        assert [(m.reference_kind, m.missing_name) for m in missing] == [("data_element", stub)]
        assert _interface_file(target, "ISig")["kind"] == "sender_receiver"
        assert _interface_file(target, stub)["external_ref"] == "/Elsewhere/ISig"

    def test_a_target_stub_of_the_leaf_with_another_external_ref_is_not_bound(self, tmp_path: Path) -> None:
        """Never bound by its leaf; and a generated root package is never stubbed, so the port keeps its refusal."""
        source = _save(tmp_path, _without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig"))
        stub = {
            "name": "ISig",
            "kind": "external_interface",
            "external_ref": "/Other/ISig",
            "external_dest": "SENDER-RECEIVER-INTERFACE",
            "data_elements": ["a"],
        }
        refusal = _refusal(source, _target(tmp_path, stub))

        assert any("unresolved reference /PortInterfaces/ISig" in line for line in refusal.skip_lines)


# What the mapper checks once the interface is known, re-run late


class TestTheUnreadInitLinesAreHeld:
    """A com-spec's ``INIT-VALUE`` under an interface the file declares nowhere is held, not told at step 2."""

    def _init_export(self, tmp_path: Path) -> str:
        source = init_project(tmp_path / "source")
        model_root = source / "model"
        _seed_types(model_root)
        write_model_element(
            model_root, {**_sig("a"), "data_elements": [{"name": "a", "implementation_type": "u8", "init": 1}]}
        )
        write_model_element(model_root, _srv("Op"))
        swc = model_root / "swc" / "body" / "Comp.yaml"
        swc.parent.mkdir(parents=True, exist_ok=True)
        swc.write_text(yaml.safe_dump(_component(), sort_keys=False), encoding="utf-8", newline="\n")
        arxml = tmp_path / "CompInit.arxml"
        export_component(str(model_root), "Comp", str(arxml))
        return _without(arxml.read_text(encoding="utf-8"), "SENDER-RECEIVER-INTERFACE", "ISig")

    def test_an_answered_path_decides_the_value_against_the_targets_element(self, tmp_path: Path) -> None:
        """The target's answer spends the held line: the file's constant is carried as the port's own value."""
        source = _save(tmp_path, self._init_export(tmp_path))
        target = _target(tmp_path, _sig("a"))

        report = import_component(source, str(target))

        assert report.swc_action == "created"
        assert not [line for line in report.not_carried_lines if line.startswith("INIT-VALUE ")]
        (port,) = [port for port in _written_swc(target)["ports"] if port["name"] == "rSig"]
        assert port["com_specs"] == [{"data_element": "a", "init": 1}]

    def test_an_ambiguous_target_refuses_before_the_held_line(self, tmp_path: Path) -> None:
        """Held: the target's own defect, met at step 4, comes first; at step 2 the line would have."""
        source = _save(tmp_path, self._init_export(tmp_path))
        target = _target(tmp_path, _sig("a"))
        duplicate = target / "interfaces" / "ISig_copy.yaml"
        duplicate.write_text(yaml.safe_dump(_sig("a"), sort_keys=False), encoding="utf-8", newline="\n")

        with pytest.raises(AmbiguousTargetProjectError):
            plan_component_import(source, str(target))


class TestTheLateChecks:
    def test_a_late_admitted_by_value_read_of_a_queued_element_is_refused(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _without(_export(tmp_path, read_by="value"), "SENDER-RECEIVER-INTERFACE", "ISig"))
        target = _target(tmp_path, _sig("a", queued=frozenset({"a"})))

        refusal = _refusal(source, target)

        assert any("rule:swc.access.queued-read-by-value" in line for line in refusal.skip_lines)
        assert refusal.missing_references == []

    def test_a_late_resolved_queued_receiver_needs_its_queue_length(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig"))
        target = _target(tmp_path, _sig("a", queued=frozenset({"a"})))

        refusal = _refusal(source, target)

        assert any(
            "no QUEUED-RECEIVER-COM-SPEC is defined for queued data element 'a'" in line for line in refusal.skip_lines
        )


class TestStepThreeARunsAgainOverTheLateAdmitted:
    """The late-admitted entries are judged after their stated names are settled."""

    LONG_PORT = "r" + "x" * 124  # R_{port}_a is 129 characters: the derived name is no Identifier

    def _long_port_export(self, tmp_path: Path, access_name: str) -> str:
        text = _export(tmp_path).replace("<SHORT-NAME>R_rSig_a</SHORT-NAME>", f"<SHORT-NAME>{access_name}</SHORT-NAME>")
        text = text.replace("rSig", self.LONG_PORT)
        return _without(text, "SENDER-RECEIVER-INTERFACE", "ISig")

    def test_a_legal_stated_name_replacing_an_illegal_derived_one_is_accepted(self, tmp_path: Path) -> None:
        source = _save(tmp_path, self._long_port_export(tmp_path, "ReadA"))
        target = _target(tmp_path, _sig("a"))

        import_component(source, str(target))

        assert _written_swc(target)["runnables"][0]["reads"] == [
            {"port": self.LONG_PORT, "element": "a", "name": "ReadA"}
        ]

    def test_an_illegal_source_name_keeps_the_derived_one_which_is_judged(self, tmp_path: Path) -> None:
        illegal = "A" * 130
        source = _save(tmp_path, self._long_port_export(tmp_path, illegal))
        target = _target(tmp_path, _sig("a"))

        with pytest.raises(ReservedNameError) as refused:
            import_component(source, str(target))

        assert f"R_{self.LONG_PORT}_a" in str(refused.value)

    def test_an_illegal_source_name_with_a_legal_derived_one_is_reported(self, tmp_path: Path) -> None:
        illegal = "A" * 130
        text = _export(tmp_path).replace("<SHORT-NAME>R_rSig_a</SHORT-NAME>", f"<SHORT-NAME>{illegal}</SHORT-NAME>")
        source = _save(tmp_path, _without(text, "SENDER-RECEIVER-INTERFACE", "ISig"))
        target = _target(tmp_path, _sig("a"))

        report = import_component(source, str(target))

        assert _written_swc(target)["runnables"][0]["reads"] == [{"port": "rSig", "element": "a"}]
        assert any(illegal in line for line in report.not_carried_lines)

    def test_a_late_insertion_keeps_every_event_source_where_it_was(self, tmp_path: Path) -> None:
        """The held operation event goes in at its source position; the timing event's stated name follows it."""
        text = _export(tmp_path)
        # Runnable S gains a timing event after its operation event, renamed so its name is stated.
        text = text.replace(
            "<EVENTS>",
            "<EVENTS><TIMING-EVENT><SHORT-NAME>Tick</SHORT-NAME>"
            '<START-ON-EVENT-REF DEST="RUNNABLE-ENTITY">/ComponentTypes/Comp/Comp_InternalBehavior/S'
            "</START-ON-EVENT-REF><PERIOD>0.02</PERIOD></TIMING-EVENT>",
            1,
        )
        operation = re.search(r"<OPERATION-INVOKED-EVENT>.*?</OPERATION-INVOKED-EVENT>", text, re.DOTALL)
        assert operation is not None
        tick = re.search(r"<TIMING-EVENT><SHORT-NAME>Tick</SHORT-NAME>.*?</TIMING-EVENT>", text, re.DOTALL)
        assert tick is not None
        text = text.replace(tick.group(0), "", 1).replace(operation.group(0), operation.group(0) + tick.group(0), 1)
        source = _save(tmp_path, _without(text, "CLIENT-SERVER-INTERFACE", "ISrv"))
        target = _target(tmp_path, _srv("Op"))

        import_component(source, str(target))

        assert _written_swc(target)["runnables"][1]["events"] == [
            {"type": "operation", "port": "pSrv", "operation": "Op"},
            {"type": "timing", "period": 0.02, "name": "Tick"},
        ]

    def test_step_3a_keeps_its_precedence_over_a_target_defect(self, tmp_path: Path) -> None:
        """An illegal name in the file is refused before an ambiguous target is even read."""
        source = _export_with_an_illegal_operation_name(tmp_path)
        target = _target(tmp_path, _sig("a"))
        duplicate = target / "interfaces" / "ISig_copy.yaml"
        duplicate.write_text(yaml.safe_dump(_sig("a"), sort_keys=False), encoding="utf-8", newline="\n")

        with pytest.raises(ReservedNameError):
            plan_component_import(source, str(target))
        # ...and with the file's defect gone, the target's own is what refuses.
        clean = _save(
            tmp_path, _without(_export(tmp_path / "again"), "SENDER-RECEIVER-INTERFACE", "ISig"), "clean.arxml"
        )
        with pytest.raises(AmbiguousTargetProjectError):
            plan_component_import(clean, str(target))

    def test_the_target_scan_for_kept_pairs_moves_no_refusal_ahead_of_step_3a(self, tmp_path: Path) -> None:
        """The tolerant scan of the target's type layers, run before ``map_data_types``, raises nothing.

        With the file clean, the target's authoritative read refuses the unparseable file by its storage name.
        """
        source = _export_with_an_illegal_operation_name(tmp_path)
        target = _target(tmp_path, _sig("a"))
        broken = target / "data_types" / "application" / "Broken.yaml"
        broken.write_text("name: [unclosed\n", encoding="utf-8", newline="\n")

        with pytest.raises(ReservedNameError):
            plan_component_import(source, str(target))
        clean = _save(
            tmp_path, _without(_export(tmp_path / "again"), "SENDER-RECEIVER-INTERFACE", "ISig"), "clean.arxml"
        )
        with pytest.raises(ElementParseError) as refused:
            plan_component_import(clean, str(target))
        assert "data_types/application/Broken.yaml" in str(refused.value)
        assert str(target) not in str(refused.value)

    def test_an_untraversable_target_type_directory_leaves_the_files_refusal_first(self, tmp_path: Path) -> None:
        """An untraversable ``data_types/`` still leaves step 3a's refusal first, never an ``OSError`` from the scan.

        A test of its own because ``read_denied`` skips where the mode bits deny nothing (root, Windows).
        """
        source = _export_with_an_illegal_operation_name(tmp_path)
        target = _target(tmp_path, _sig("a"))

        with read_denied(target / "data_types"), pytest.raises(ReservedNameError):
            plan_component_import(source, str(target))


# The gate: the descriptive part written, runnables and unresolved sets withheld


def _with_mapping_set_reference(text: str) -> str:
    """*text* with a ``DATA-TYPE-MAPPING-REF`` onto a set neither the file nor any target declares."""
    return text.replace(
        "<HANDLE-TERMINATION-AND-RESTART>",
        '<DATA-TYPE-MAPPING-REFS><DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">/Elsewhere/Maps'
        "</DATA-TYPE-MAPPING-REF></DATA-TYPE-MAPPING-REFS><HANDLE-TERMINATION-AND-RESTART>",
        1,
    )


_PHYSICAL_TYPES = ({"name": "Spd_T", "kind": "application_type", "unit": "km/h"},)
_SPEED_MAPS = {"name": "Maps", "maps": [{"application": "Spd_T", "implementation": "u8"}]}


def _physical_export(tmp_path: Path) -> str:
    """SWCraft's own export of ``Comp``: port ``rSpeed`` speaks ``Spd_T``, mapped by ``Maps``; it serves ``ISrv``."""
    source = init_project(tmp_path / "physical-source")
    model_root = source / "model"
    _seed_types(model_root)
    for element in _PHYSICAL_TYPES:
        write_model_element(model_root, element)
    write_model_element(model_root, _SPEED_MAPS, layer="mapping_sets")
    write_model_element(
        model_root,
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "speed", "application_type": "Spd_T"}],
        },
    )
    write_model_element(model_root, _srv("Op"))
    component = {
        "name": "Comp",
        "type": "application",
        "ports": [
            {"name": "rSpeed", "direction": "require", "interface": "ISpeed"},
            {"name": "pSrv", "direction": "provide", "interface": "ISrv"},
        ],
        "runnables": [{"name": "S", "events": [{"type": "operation", "port": "pSrv", "operation": "Op"}]}],
        "data_type_mappings": ["Maps"],
        "handle_termination_and_restart": "no_support",
    }
    swc = model_root / "swc" / "body" / "Comp.yaml"
    swc.parent.mkdir(parents=True, exist_ok=True)
    swc.write_text(yaml.safe_dump(component, sort_keys=False), encoding="utf-8", newline="\n")
    arxml = tmp_path / "Physical.arxml"
    export_component(str(model_root), "Comp", str(arxml))
    return arxml.read_text(encoding="utf-8")


class TestTheGateWithholdsTheBehaviour:
    """A verdict creates the component without ``runnables`` and its unresolved mapping sets; nothing else moves."""

    def test_a_withheld_create_passes_check_model(self, tmp_path: Path) -> None:
        """The file's ``handle_termination_and_restart`` is written, so the build accepts the project."""
        text = _with_mapping_set_reference(
            _without_com_specs(_without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig"))
        )
        target = _target(tmp_path, _sig("other"))

        missing, written = _withheld(_save(tmp_path, text), target)

        assert [m.reference_kind for m in missing] == ["data_element", "mapping_set"]
        assert [port["name"] for port in written["ports"]] == ["pSrv", "rSig"]
        assert written.get("data_type_mappings", []) == [], "the one set nothing answers is withheld"
        _link_and_check(target)

    def test_the_withheld_fields_are_the_only_ones_the_gate_touches(self, tmp_path: Path) -> None:
        """Imported into a target that answers it and one that does not, the file differs in two keys.

        The withheld import keeps the resolved ``Maps``, drops the unresolved ``Gone`` and empties
        ``runnables``; a file naming no set would witness nothing.
        """
        text = _with_mapping_set_reference(
            _without_com_specs(_without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig"))
        ).replace(
            "</DATA-TYPE-MAPPING-REF></DATA-TYPE-MAPPING-REFS>",
            '</DATA-TYPE-MAPPING-REF><DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">/Elsewhere/Gone'
            "</DATA-TYPE-MAPPING-REF></DATA-TYPE-MAPPING-REFS>",
            1,
        )
        source = _save(tmp_path, text)
        complete = _target(tmp_path, _sig("a"), name="complete")
        write_model_element(complete, {"name": "Maps", "maps": []}, layer="mapping_sets")
        write_model_element(complete, {"name": "Gone", "maps": []}, layer="mapping_sets")
        import_component(source, str(complete))
        withheld = _target(tmp_path, _sig("other"), name="withheld")
        write_model_element(withheld, {"name": "Maps", "maps": []}, layer="mapping_sets")

        missing, written = _withheld(source, withheld)

        answered = _written_swc(complete)
        assert [(m.reference_kind, m.missing_name) for m in missing] == [
            ("data_element", "ISig"),
            ("mapping_set", "Gone"),
        ]
        assert answered["runnables"], "the answered import keeps its behaviour"
        assert answered["data_type_mappings"] == ["Maps", "Gone"]
        assert written["data_type_mappings"] == ["Maps"], "the resolved set is kept, the unresolved one dropped"
        withheld_keys = {"runnables", "data_type_mappings"}
        assert {key: value for key, value in answered.items() if key not in withheld_keys} == {
            key: value for key, value in written.items() if key not in withheld_keys
        }

    @pytest.mark.parametrize("answered_by", ["file", "target"])
    def test_a_withheld_create_keeps_a_resolved_set_and_the_build_accepts_it(
        self, tmp_path: Path, answered_by: str
    ) -> None:
        """A port speaking an application type, its set resolved: ``check-model`` and ``generate arxml`` accept.

        A withheld resolved set would leave ``rule:data-type.application-type-unmapped`` standing.
        """
        text = _without_com_specs(_without(_physical_export(tmp_path), "CLIENT-SERVER-INTERFACE", "ISrv"))
        target = _target(tmp_path, _srv("Other"))
        if answered_by == "target":
            text = _without(text, "DATA-TYPE-MAPPING-SET", "Maps")
            for element in _PHYSICAL_TYPES:
                write_model_element(target, element)
            write_model_element(target, _SPEED_MAPS, layer="mapping_sets")

        missing, written = _withheld(_save(tmp_path, text), target)

        assert [m.reference_kind for m in missing] == ["operation"]
        assert written["data_type_mappings"] == ["Maps"]
        _link_and_check(target)
        arxml = tmp_path / "built.arxml"
        generate_arxml(str(target), str(arxml))
        assert "<SHORT-NAME>Maps</SHORT-NAME>" in arxml.read_text(encoding="utf-8")

    @pytest.mark.parametrize("verdict", [False, True], ids=["answered", "withheld"])
    def test_a_withheld_behaviour_reports_none_of_its_budgets(self, tmp_path: Path, verdict: bool) -> None:
        """A component written without runnables emits no ``SWC-TIMING``, so no line names one.

        The export's budget gets a normalisation line (CSE code 27), a name line and a descriptive
        line (a ``MINIMUM``); withheld, all three go with the runnables.
        """
        text, count = re.subn(
            r"<CSE-CODE>26</CSE-CODE>\s*<CSE-CODE-FACTOR>850</CSE-CODE-FACTOR>\s*</MAXIMUM>",
            "<CSE-CODE>27</CSE-CODE><CSE-CODE-FACTOR>85</CSE-CODE-FACTOR></MAXIMUM>"
            "<MINIMUM><CSE-CODE>26</CSE-CODE><CSE-CODE-FACTOR>100</CSE-CODE-FACTOR></MINIMUM>",
            _without_com_specs(_export(tmp_path, wcet="850ns")),
        )
        assert count == 1
        text = text.replace("<SHORT-NAME>Comp_Timing</SHORT-NAME>", "<SHORT-NAME>Foreign_Timing</SHORT-NAME>")
        if verdict:
            text = _without(text, "SENDER-RECEIVER-INTERFACE", "ISig")
            _withheld(_save(tmp_path, text, "planned.arxml"), _target(tmp_path, _sig("other"), name="planned"))
        source = _save(tmp_path, text)

        rendered = import_component(source, str(_target(tmp_path, _sig("other") if verdict else _sig("a")))).render()

        for fragment in ("represented as wcet 850ns", "MINIMUM", "Foreign_Timing"):
            assert (fragment in rendered) is not verdict, (fragment, rendered)

    def test_no_verdict_writes_the_behaviour_and_reports_none(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _without_com_specs(_without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig")))
        target = _target(tmp_path, _sig("a"))

        planned = plan_component_import(source, str(target))
        report = import_component(source, str(target))

        assert planned.missing_references == ()
        assert report.missing_references == []
        assert "References the project does not have" not in report.render()
        assert _written_swc(target)["runnables"][0]["reads"] == [{"port": "rSig", "element": "a"}]

    def test_the_report_names_the_withheld_fields_and_each_verdict(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _without_com_specs(_without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig")))
        target = _target(tmp_path, _sig("other"))

        report = import_component(source, str(target))

        rendered = report.render().splitlines()
        heading = next(line for line in rendered if line.startswith("References the project does not have"))
        assert "runnables and any mapping set listed below are withheld" in heading
        block = rendered[rendered.index(heading) + 1 :][: len(report.missing_references)]
        assert block == [f"  - {line}" for line in report.missing_references]
        assert all("/" not in line for line in block), "the block names no ARXML path and no storage path"

    def test_an_unsupported_construct_still_refuses_the_import_whole(self, tmp_path: Path) -> None:
        text = _without_com_specs(_without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig")).replace(
            "</PORTS>", "<PR-PORT-PROTOTYPE><SHORT-NAME>prX</SHORT-NAME></PR-PORT-PROTOTYPE></PORTS>", 1
        )

        refusal = _refusal(_save(tmp_path, text), _target(tmp_path, _sig("other")))

        assert any("PR-PORT-PROTOTYPE" in line for line in refusal.skip_lines)

    def test_an_illegal_runnable_name_still_refuses_with_a_withheld_behaviour(self, tmp_path: Path) -> None:
        """Step 3a's first run judges the file's own names and stands, whatever the gate would withhold."""
        illegal = "S" * 130
        text = _export(tmp_path).replace("<SHORT-NAME>S</SHORT-NAME>", f"<SHORT-NAME>{illegal}</SHORT-NAME>", 1)
        text = text.replace("/Comp_InternalBehavior/S<", f"/Comp_InternalBehavior/{illegal}<")
        source = _save(tmp_path, _without_com_specs(_without(text, "SENDER-RECEIVER-INTERFACE", "ISig")))
        target = _target(tmp_path, _sig("other"))
        before = files_of(target)

        with pytest.raises(ReservedNameError) as refused:
            import_component(source, str(target))

        assert illegal in str(refused.value)
        assert files_of(target) == before

    def test_a_verdict_on_a_component_the_target_holds_keeps_the_projects_behaviour(self, tmp_path: Path) -> None:
        """The project's behaviour is kept and the equal descriptive part makes the import ``unchanged``."""
        source = _save(tmp_path, _without_com_specs(_without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig")))
        target = _target(tmp_path, _sig("other"))
        held = target / "swc" / "components" / "Comp.yaml"
        held.parent.mkdir(parents=True, exist_ok=True)
        held.write_text(yaml.safe_dump(_component(), sort_keys=False), encoding="utf-8", newline="\n")
        before = held.read_bytes()

        planned = plan_component_import(source, str(target))
        report = import_component(source, str(target))

        assert [m.reference_kind for m in planned.missing_references] == ["data_element"]
        assert report.swc_action == "unchanged"
        assert held.read_bytes() == before


class TestTheLinesComputedFromTheBehaviourAreRecomputed:
    """Runnable-attributable losses go with a withheld behaviour; the descriptive ones stay, once."""

    def test_an_event_name_line_goes_and_the_behaviour_name_line_stays_once(self, tmp_path: Path) -> None:
        # An illegal event SHORT-NAME is not stated but reported as a line; so is an illegal behaviour one.
        text = _export(tmp_path).replace("<SHORT-NAME>TMT_R</SHORT-NAME>", "<SHORT-NAME>1Tick</SHORT-NAME>", 1)
        text = text.replace("Comp_InternalBehavior", "1Behaviour")
        source = _save(tmp_path, _without_com_specs(_without(text, "SENDER-RECEIVER-INTERFACE", "ISig")))

        answered = import_component(source, str(_target(tmp_path, _sig("a"), name="answered")))
        withheld = import_component(source, str(_target(tmp_path, _sig("other"), name="withheld")))

        assert any("1Tick" in line for line in answered.not_carried_lines)
        assert not any("1Tick" in line for line in withheld.not_carried_lines)
        # The behaviour's own line, keyed at its path: the behaviour is written, so its line stays.
        behaviour_lines = [line for line in withheld.not_carried_lines if "/Comp/1Behaviour:" in line]
        assert len(behaviour_lines) == 1
        assert behaviour_lines == [line for line in answered.not_carried_lines if "/Comp/1Behaviour:" in line]

    def test_a_mapped_line_below_the_behaviour_goes_and_a_ports_line_stays(self, tmp_path: Path) -> None:
        """A runnable's ``UUID`` line (the reader's, at the runnable's path) describes content the write withholds."""
        text = _export(tmp_path).replace(
            "<RUNNABLE-ENTITY>", '<RUNNABLE-ENTITY UUID="0b7f3c1e-0000-4000-8000-000000000001">', 1
        )
        text = text.replace("<P-PORT-PROTOTYPE>", '<P-PORT-PROTOTYPE UUID="0b7f3c1e-0000-4000-8000-000000000002">', 1)
        source = _save(tmp_path, _without_com_specs(_without(text, "SENDER-RECEIVER-INTERFACE", "ISig")))

        answered = import_component(source, str(_target(tmp_path, _sig("a"), name="answered")))
        withheld = import_component(source, str(_target(tmp_path, _sig("other"), name="withheld")))

        assert any(
            line.startswith("UUID /ComponentTypes/Comp/Comp_InternalBehavior/R:") for line in answered.not_carried_lines
        )
        assert not any("/Comp_InternalBehavior/" in line for line in withheld.not_carried_lines)
        port_lines = [line for line in answered.not_carried_lines if "/Comp/pSrv:" in line]
        assert port_lines
        assert port_lines == [line for line in withheld.not_carried_lines if "/Comp/pSrv:" in line]

    def test_an_access_collapse_goes_with_the_withheld_runnable(self, tmp_path: Path) -> None:
        """Two reads of one ``(port, element)`` collapse to one, a report line; withheld, the runnable is gone."""
        text = _export(tmp_path)
        access = re.search(r"<VARIABLE-ACCESS>.*?</VARIABLE-ACCESS>", text, re.DOTALL)
        assert access is not None
        twin = access.group(0).replace("<SHORT-NAME>R_rSig_a</SHORT-NAME>", "<SHORT-NAME>Again</SHORT-NAME>")
        text = text.replace(access.group(0), access.group(0) + twin, 1)
        # A port the file answers itself, so the collapse is the mapper's and not the late pass's.
        text = _without_com_specs(_without(text, "CLIENT-SERVER-INTERFACE", "ISrv"))
        source = _save(tmp_path, text)

        answered = import_component(source, str(_target(tmp_path, _srv("Op"), name="answered")))
        withheld = import_component(source, str(_target(tmp_path, _srv("Other"), name="withheld")))

        assert answered.access_collapse_lines
        assert withheld.missing_references
        assert withheld.access_collapse_lines == []


# The stub path: reuse by `external_ref`, else mint


def _elsewhere(text: str, interface: str) -> str:
    """*text* without its declaration of *interface*, every reference to it moved to ``/Elsewhere/{interface}``.

    Outside the writer's root packages, where the stub rule admits a stub.
    """
    tag = "SENDER-RECEIVER-INTERFACE" if interface == "ISig" else "CLIENT-SERVER-INTERFACE"
    return _without(text, tag, interface).replace(f"/PortInterfaces/{interface}", f"/Elsewhere/{interface}")


def _port(name: str, path: str, dest: str = "SENDER-RECEIVER-INTERFACE") -> str:
    """One require port on *path*, with no com-spec."""
    return (
        f"<R-PORT-PROTOTYPE><SHORT-NAME>{name}</SHORT-NAME>"
        f'<REQUIRED-INTERFACE-TREF DEST="{dest}">{path}</REQUIRED-INTERFACE-TREF></R-PORT-PROTOTYPE>'
    )


def _with_ports(text: str, *ports: str) -> str:
    return text.replace("</PORTS>", "".join(ports) + "</PORTS>", 1)


def _target_stub(name: str, external_ref: str, *members: str, dest: str = "SENDER-RECEIVER-INTERFACE") -> dict:
    stub: dict = {"name": name, "kind": "external_interface", "external_ref": external_ref, "external_dest": dest}
    if members:
        stub["operations" if dest == "CLIENT-SERVER-INTERFACE" else "data_elements"] = list(members)
    return stub


def _with_disabled_mode(text: str) -> str:
    """*text* with a require port ``rMode`` on ``/Elsewhere/IMode`` and runnable ``R``'s timing event disabled in it."""
    text = _with_ports(text, _port("rMode", "/Elsewhere/IMode", dest="MODE-SWITCH-INTERFACE"))
    return text.replace(
        "<SHORT-NAME>TMT_R</SHORT-NAME>",
        "<SHORT-NAME>TMT_R</SHORT-NAME><DISABLED-MODE-IREFS><DISABLED-MODE-IREF>"
        '<CONTEXT-PORT-REF DEST="R-PORT-PROTOTYPE">/ComponentTypes/Comp/rMode</CONTEXT-PORT-REF>'
        '<CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF DEST="MODE-DECLARATION-GROUP-PROTOTYPE">/Elsewhere/IMode/mg'
        "</CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF>"
        '<TARGET-MODE-DECLARATION-REF DEST="MODE-DECLARATION">/Elsewhere/Modes/OFF</TARGET-MODE-DECLARATION-REF>'
        "</DISABLED-MODE-IREF></DISABLED-MODE-IREFS>",
        1,
    )


class TestAPathNoTierAnswersBecomesAStub:
    """A target stub of the same ``external_ref`` reused, else a stub minted as ``import model`` mints one.

    Stub names are asserted through ``stub_name`` with the names the import claims, never as a literal.
    """

    def test_the_stub_is_written_with_the_source_path_and_its_com_spec_carried(self, tmp_path: Path) -> None:
        target = _target(tmp_path)

        missing, written = _withheld(_save(tmp_path, _elsewhere(_export(tmp_path), "ISig")), target)

        stub = stub_name("ISig", 0, {"ISrv"})[0]
        assert {"name": "rSig", "direction": "require", "interface": stub} in written["ports"]
        assert _interface_file(target, stub) == {
            "name": stub,
            "kind": "external_interface",
            "external_ref": "/Elsewhere/ISig",
            "external_dest": "SENDER-RECEIVER-INTERFACE",
            # From the port's receiver com-spec, the one source of a member on this path.
            "data_elements": ["a"],
        }
        # The read through the stub port is a verdict whatever the stub lists.
        assert [(m.reference_kind, m.missing_member, m.missing_name) for m in missing] == [("data_element", "a", stub)]
        _link_and_check(target)

    def test_the_created_list_names_the_minted_stub(self, tmp_path: Path) -> None:
        report = import_component(_save(tmp_path, _elsewhere(_export(tmp_path), "ISig")), str(_target(tmp_path)))

        assert sorted(report.created_interfaces) == sorted(["ISrv", stub_name("ISig", 0, {"ISrv"})[0]])
        assert report.reused_interfaces == []

    def test_a_behavioural_reference_lists_no_member(self, tmp_path: Path) -> None:
        """Without a com-spec the stub lists nothing: the read is a verdict, never the stub's evidence."""
        target = _target(tmp_path)
        source = _save(tmp_path, _without_com_specs(_elsewhere(_export(tmp_path), "ISig")))

        missing, _written = _withheld(source, target)

        stub = stub_name("ISig", 0, {"ISrv"})[0]
        assert "data_elements" not in _interface_file(target, stub)
        assert [(m.reference_kind, m.missing_member, m.missing_name) for m in missing] == [("data_element", "a", stub)]

    def test_an_operation_event_through_a_fresh_stub_port_is_a_verdict(self, tmp_path: Path) -> None:
        target = _target(tmp_path)

        missing, _written = _withheld(_save(tmp_path, _elsewhere(_export(tmp_path), "ISrv")), target)

        stub = stub_name("ISrv", 0, {"ISig"})[0]
        assert [(m.reference_kind, m.missing_member, m.missing_name) for m in missing] == [("operation", "Op", stub)]
        # The server com-spec lists the operation: the stub is descriptive, the event still a verdict.
        assert _interface_file(target, stub)["operations"] == ["Op"]

    def test_a_target_stub_of_the_same_external_ref_is_reused_and_nothing_minted(self, tmp_path: Path) -> None:
        target = _target(tmp_path, _target_stub("ISigStub", "/Elsewhere/ISig", "a"))
        before = (target / "interfaces" / "ISigStub.yaml").read_bytes()
        source = _save(tmp_path, _elsewhere(_export(tmp_path), "ISig"))

        planned = plan_component_import(source, str(target))
        report = import_component(source, str(target))

        written = _written_swc(target)
        assert {"name": "rSig", "direction": "require", "interface": "ISigStub"} in written["ports"]
        assert written.get("runnables", []) == []
        assert sorted(path.name for path in (target / "interfaces").iterdir()) == ["ISigStub.yaml", "ISrv.yaml"]
        assert (target / "interfaces" / "ISigStub.yaml").read_bytes() == before, "a reused stub is not rewritten"
        # The stub lists `a`, an earlier import's inference: the read through it is still a verdict.
        assert [(m.reference_kind, m.missing_member, m.missing_name) for m in planned.missing_references] == [
            ("data_element", "a", "ISigStub")
        ]
        assert report.created_interfaces == ["ISrv"]
        assert report.reused_interfaces == ["ISigStub"]

    def test_a_com_spec_on_a_reused_stub_without_the_member_is_a_line_and_the_stub_unchanged(
        self, tmp_path: Path
    ) -> None:
        """A com-spec naming a member the reused stub does not list is a *Not carried* line, never a member added."""
        target = _target(tmp_path, _target_stub("ISigStub", "/Elsewhere/ISig", "b"))
        source = _save(tmp_path, _elsewhere(_export(tmp_path), "ISig"))

        report = plan_component_import(source, str(target)).report
        missing, written = _withheld(source, target)

        assert any(
            line.startswith("NONQUEUED-RECEIVER-COM-SPEC /ComponentTypes/Comp/rSig/REQUIRED-COM-SPECS")
            and "names no member the stub lists" in line
            for line in report.not_carried_lines
        )
        assert [(m.reference_kind, m.missing_member, m.missing_name) for m in missing] == [
            ("data_element", "a", "ISigStub")
        ]
        assert {"name": "rSig", "direction": "require", "interface": "ISigStub"} in written["ports"]
        assert _interface_file(target, "ISigStub")["data_elements"] == ["b"]

    def test_a_same_named_stub_with_another_external_ref_takes_the_next_name(self, tmp_path: Path) -> None:
        """The claimed names include the target's, so a divergent shared definition is never reached."""
        target = _target(tmp_path, _target_stub("ISig", "/Other/ISig", "a"))

        _missing, written = _withheld(_save(tmp_path, _elsewhere(_export(tmp_path), "ISig")), target)

        stub = stub_name("ISig", 0, {"ISig", "ISrv"})[0]
        assert stub != "ISig"
        assert {"name": "rSig", "direction": "require", "interface": stub} in written["ports"]
        assert _interface_file(target, "ISig")["external_ref"] == "/Other/ISig"
        assert _interface_file(target, stub)["external_ref"] == "/Elsewhere/ISig"

    def test_a_mixed_same_leaf_set_consumes_no_rank_for_the_answered_and_the_reused_path(self, tmp_path: Path) -> None:
        """One leaf, three paths: the file answers one, the target's stub another, the third is minted.

        The paths sort answered < reused < minted, so a rank either of the first two took would move
        the minted stub's name.
        """
        text = _with_ports(
            _without_com_specs(_export(tmp_path)),
            _port("rReused", "/Reused/ISig"),
            _port("rMinted", "/Zed/ISig"),
        )
        target = _target(tmp_path, _target_stub("ISigOld", "/Reused/ISig"))

        report = import_component(_save(tmp_path, text), str(target))

        minted = stub_name("ISig", 0, {"ISig", "ISrv", "ISigOld"})[0]
        written = _written_swc(target)
        assert {port["name"]: port["interface"] for port in written["ports"]} == {
            "pSrv": "ISrv",
            "rSig": "ISig",
            "rReused": "ISigOld",
            "rMinted": minted,
        }
        assert sorted(report.created_interfaces) == sorted(["ISig", "ISrv", minted])
        assert report.reused_interfaces == ["ISigOld"]
        assert report.missing_references == [], "no behaviour goes through the two stub ports"
        assert written["runnables"], "the behaviour is written"

    def test_two_referrers_disagreeing_on_dest_are_refused_as_before(self, tmp_path: Path) -> None:
        text = _with_ports(
            _without_com_specs(_export(tmp_path)),
            _port("rOne", "/Elsewhere/IX"),
            _port("rTwo", "/Elsewhere/IX", dest="CLIENT-SERVER-INTERFACE"),
        )

        refusal = _refusal(_save(tmp_path, text), _target(tmp_path))

        refused = {
            port
            for port in ("rOne", "rTwo")
            for line in refusal.skip_lines
            if f"/ComponentTypes/Comp/{port}" in line and "unresolved reference /Elsewhere/IX" in line
        }
        assert refused == {"rOne", "rTwo"}

    def test_an_access_through_a_reused_stub_port_is_a_verdict_and_lists_nothing(self, tmp_path: Path) -> None:
        target = _target(tmp_path, _target_stub("ISigStub", "/Elsewhere/ISig"))
        source = _save(tmp_path, _without_com_specs(_elsewhere(_export(tmp_path), "ISig")))

        missing, _written = _withheld(source, target)

        assert [(m.reference_kind, m.missing_member, m.missing_name) for m in missing] == [
            ("data_element", "a", "ISigStub")
        ]
        assert "data_elements" not in _interface_file(target, "ISigStub")

    def test_a_disabled_mode_through_a_reused_stub_port_is_a_verdict(self, tmp_path: Path) -> None:
        target = _target(tmp_path, _target_stub("IModeStub", "/Elsewhere/IMode", dest="MODE-SWITCH-INTERFACE"))

        missing, written = _withheld(_save(tmp_path, _with_disabled_mode(_export(tmp_path))), target)

        assert [(m.reference_kind, m.missing_member, m.missing_name) for m in missing] == [
            ("mode_group", "mg", "IModeStub")
        ]
        assert {"name": "rMode", "direction": "require", "interface": "IModeStub"} in written["ports"]

    def test_an_over_length_leaf_is_refused(self, tmp_path: Path) -> None:
        """A leaf no Identifier can be: the stub rule declines the path (``REF_PATH``); the port keeps its refusal."""
        leaf = "I" + "x" * 128
        text = _with_ports(_without_com_specs(_export(tmp_path)), _port("rLong", f"/Elsewhere/{leaf}"))

        refusal = _refusal(_save(tmp_path, text), _target(tmp_path))

        assert any(
            "/ComponentTypes/Comp/rLong" in line and f"unresolved reference /Elsewhere/{leaf}" in line
            for line in refusal.skip_lines
        )

    def test_swcrafts_own_export_of_the_stubbed_component_is_still_refused(self, tmp_path: Path) -> None:
        """Not reached: the exporter refuses a closure reaching a stub, so a stub does not round-trip."""
        target = _target(tmp_path)
        _withheld(_save(tmp_path, _elsewhere(_export(tmp_path), "ISig")), target)

        with pytest.raises(ComponentExchangeError, match="external interface"):
            export_component(str(target), "Comp", str(tmp_path / "again.arxml"))
        assert not (tmp_path / "again.arxml").exists()

    def test_import_model_still_lists_the_member_a_behavioural_reference_names(self, tmp_path: Path) -> None:
        """The late policy alone stages no behavioural member: ``import model``'s stub still lists the read's."""
        source = _save(tmp_path, _without_com_specs(_elsewhere(_export(tmp_path), "ISig")))
        project = init_project(tmp_path / "whole")

        report = run_import(source, str(project / "swcraft.yaml"))

        name = report.external_interface_stubs[0].name
        stub = yaml.safe_load((project / "model" / "interfaces" / f"{name}.yaml").read_text(encoding="utf-8"))
        assert stub["data_elements"] == ["a"]


# Inits and com-specs after resolution, com-spec losses as lines

_GEAR = {"name": "Gear_T", "kind": "enum", "literals": [{"name": "PARK", "value": 0}, {"name": "DRIVE", "value": 1}]}
_POSE = {"name": "Pose_T", "kind": "record", "fields": [{"name": "x", "type": "Gear_T"}]}


def _valued_export(
    tmp_path: Path, *, com_spec: dict | None = None, queued: bool = False, element_init: int | None = 1
) -> str:
    """SWCraft's export of ``Comp`` whose port ``rSig`` states *com_spec*, with ``ISig`` (``a``, ``u8``) in the file.

    ``a`` states *element_init*, so the file declares the constant ``/PortInterfaces/ISig_a_Init``,
    which a com-spec without an ``init`` of its own references.
    """
    source = init_project(tmp_path / "source")
    model_root = source / "model"
    _seed_types(model_root)
    element: dict = {"name": "a", "implementation_type": "u8"}
    if element_init is not None:
        element["init"] = element_init
    if queued:
        element["sw_impl_policy"] = "queued"
    write_model_element(model_root, {"name": "ISig", "kind": "sender_receiver", "data_elements": [element]})
    write_model_element(model_root, _srv("Op"))
    component = _component()
    if com_spec is not None:
        component["ports"][0]["com_specs"] = [{"data_element": "a", **com_spec}]
    swc = model_root / "swc" / "body" / "Comp.yaml"
    swc.parent.mkdir(parents=True, exist_ok=True)
    swc.write_text(yaml.safe_dump(component, sort_keys=False), encoding="utf-8", newline="\n")
    arxml = tmp_path / "Valued.arxml"
    export_component(str(model_root), "Comp", str(arxml))
    return arxml.read_text(encoding="utf-8")


def _port_of(model_root: Path, name: str = "rSig") -> dict:
    (port,) = [port for port in _written_swc(model_root)["ports"] if port["name"] == name]
    return port


def _init_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("INIT-VALUE ")]


class TestInitsAndComSpecsAfterResolution:
    """A target-resolved port's com-specs and init judged against the target's interface.

    A value loss is a line under *Not carried*, the port written; a com-spec naming a member the
    answering interface lacks is a verdict.
    """

    def test_an_own_init_on_a_target_resolved_port_is_carried(self, tmp_path: Path) -> None:
        text = _without(_valued_export(tmp_path, com_spec={"init": 5}), "SENDER-RECEIVER-INTERFACE", "ISig")
        target = _target(tmp_path, {**_sig("a"), "data_elements": [{"name": "a", "implementation_type": "u8"}]})

        report = import_component(_save(tmp_path, text), str(target))

        assert report.swc_action == "created"
        assert report.missing_references == []
        assert _init_lines(report.not_carried_lines) == []
        assert _port_of(target)["com_specs"] == [{"data_element": "a", "init": 5}]
        assert _written_swc(target)["runnables"], "the behaviour is written"

    def test_an_own_init_equal_to_the_targets_element_is_carried_by_equivalence(self, tmp_path: Path) -> None:
        text = _without(_valued_export(tmp_path, com_spec={"init": 5}), "SENDER-RECEIVER-INTERFACE", "ISig")
        target = _target(
            tmp_path, {**_sig("a"), "data_elements": [{"name": "a", "implementation_type": "u8", "init": 5}]}
        )

        report = import_component(_save(tmp_path, text), str(target))

        assert _init_lines(report.not_carried_lines) == []
        assert "com_specs" not in _port_of(target)

    def test_a_queued_element_on_a_target_resolved_port_is_carried(self, tmp_path: Path) -> None:
        text = _without(
            _valued_export(tmp_path, com_spec={"queue_length": 3}, queued=True), "SENDER-RECEIVER-INTERFACE", "ISig"
        )
        target = _target(tmp_path, _sig("a", queued=frozenset({"a"})))

        report = import_component(_save(tmp_path, text), str(target))

        assert report.swc_action == "created"
        assert _port_of(target)["com_specs"] == [{"data_element": "a", "queue_length": 3}]

    def test_a_constant_ref_to_a_constant_the_file_lacks_is_a_line_not_a_value(self, tmp_path: Path) -> None:
        """The target's own ``{I}_{E}_Init`` is no constant of the file: the reference is a line, never the value."""
        text = _without(_valued_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig")
        text = _without(text, "CONSTANT-SPECIFICATION", "ISig_a_Init")
        target = _target(
            tmp_path, {**_sig("a"), "data_elements": [{"name": "a", "implementation_type": "u8", "init": 1}]}
        )

        report = import_component(_save(tmp_path, text), str(target))

        assert report.swc_action == "created"
        (line,) = _init_lines(report.not_carried_lines)
        assert line.startswith("INIT-VALUE /ComponentTypes/Comp/rSig/REQUIRED-COM-SPECS")
        assert "'/PortInterfaces/ISig_a_Init', which is not a CONSTANT-SPECIFICATION in this file" in line
        assert "com_specs" not in _port_of(target)

    @pytest.mark.parametrize(
        ("element", "types", "words"),
        [
            pytest.param(
                {"name": "a", "implementation_type": "Nope_T"},
                (),
                "declared type 'Nope_T' is not among the mapped data types",
                id="a-missing-type",
            ),
            pytest.param(
                {"name": "a", "application_type": "Gear_T"},
                (_GEAR,),
                "text 'ON' names no literal of enum type 'Gear_T' (declared literals: PARK=0, DRIVE=1)",
                id="an-unknown-enum-literal",
            ),
            pytest.param(
                {"name": "a", "application_type": "Pose_T"},
                (_GEAR, _POSE),
                "record type 'Pose_T' needs a RECORD-VALUE-SPECIFICATION",
                id="a-wrong-record-shape",
            ),
            pytest.param(
                {"name": "a", "application_type": "Arr_T"},
                (_GEAR, {"name": "Arr_T", "kind": "array", "element_type": "Gear_T", "length": 2}),
                "array type 'Arr_T' needs an ARRAY-VALUE-SPECIFICATION",
                id="a-wrong-array-shape",
            ),
            pytest.param(
                {"name": "a", "implementation_type": "Ptr_T"},
                ({"name": "Ptr_T", "kind": "pointer", "target": "u8"},),
                "pointer type 'Ptr_T' takes no init value",
                id="a-pointer-init",
            ),
        ],
    )
    def test_a_value_the_targets_type_cannot_take_is_its_specific_line(
        self, tmp_path: Path, element: dict, types: tuple[dict, ...], words: str
    ) -> None:
        """Each reason ``_map_init_value`` gives, against the target element's type -- never a blanket line."""
        text = _without(_valued_export(tmp_path, com_spec={"init": 5}), "SENDER-RECEIVER-INTERFACE", "ISig")
        text = re.sub(
            r"<NUMERICAL-VALUE-SPECIFICATION>\s*<VALUE>5</VALUE>\s*</NUMERICAL-VALUE-SPECIFICATION>",
            "<TEXT-VALUE-SPECIFICATION><VALUE>ON</VALUE></TEXT-VALUE-SPECIFICATION>",
            text,
        )
        target = _target(tmp_path, *types, {**_sig("a"), "data_elements": [element]})

        report = import_component(_save(tmp_path, text), str(target))

        assert report.swc_action == "created"
        (line,) = _init_lines(report.not_carried_lines)
        assert "INIT-VALUE of a com-spec not carried: data element 'a': " in line
        assert words in line
        assert "com_specs" not in _port_of(target)

    def test_an_init_value_on_a_stub_port_is_a_line_and_the_port_written(self, tmp_path: Path) -> None:
        target = _target(tmp_path)
        source = _save(tmp_path, _elsewhere(_valued_export(tmp_path), "ISig"))
        report = plan_component_import(source, str(target)).report

        _missing, written = _withheld(source, target)

        stub = stub_name("ISig", 0, {"ISrv"})[0]
        (line,) = _init_lines(report.not_carried_lines)
        assert line.startswith("INIT-VALUE /ComponentTypes/Comp/rSig/REQUIRED-COM-SPECS")
        assert f"a member of the external interface stub '{stub}' (/Elsewhere/ISig)" in line
        assert {"name": "rSig", "direction": "require", "interface": stub} in written["ports"]

    def test_a_com_spec_child_swcraft_does_not_carry_is_one_line_and_the_port_written(self, tmp_path: Path) -> None:
        text = _without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig")
        text = text.replace(
            "</NONQUEUED-RECEIVER-COM-SPEC>",
            "<HANDLE-DATA-STATUS>true</HANDLE-DATA-STATUS></NONQUEUED-RECEIVER-COM-SPEC>",
        )
        target = _target(tmp_path, _sig("a"))

        report = import_component(_save(tmp_path, text), str(target))

        assert report.swc_action == "created"
        assert [line.split(" ", 2)[:2] for line in report.not_carried_lines] == [
            ["HANDLE-DATA-STATUS", "/ComponentTypes/Comp/rSig/REQUIRED-COM-SPECS:"]
        ]
        assert _port_of(target)["interface"] == "ISig"
        assert _written_swc(target)["runnables"], "a line withholds nothing"

    def test_a_com_spec_naming_a_data_element_the_targets_interface_lacks_is_a_verdict(self, tmp_path: Path) -> None:
        """With the com-specs kept, the com-spec is a verdict beside the read."""
        source = _save(tmp_path, _without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig"))

        missing, written = _withheld(source, _target(tmp_path, _sig("other")))

        assert [(m.referrer, m.reference_kind, m.missing_member, m.missing_name) for m in missing] == [
            ("runnable 'R' read through port 'rSig'", "data_element", "a", "ISig"),
            ("port 'rSig' NONQUEUED-RECEIVER-COM-SPEC", "data_element", "a", "ISig"),
        ]
        assert {"name": "rSig", "direction": "require", "interface": "ISig"} in written["ports"]

    def test_a_com_spec_naming_an_operation_the_targets_interface_lacks_is_a_verdict(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _without(_export(tmp_path), "CLIENT-SERVER-INTERFACE", "ISrv"))

        missing, _written = _withheld(source, _target(tmp_path, _srv("Other")))

        assert [(m.referrer, m.reference_kind, m.missing_member, m.missing_name) for m in missing] == [
            ("runnable 'S' operation event through port 'pSrv'", "operation", "Op", "ISrv"),
            ("port 'pSrv' SERVER-COM-SPEC", "operation", "Op", "ISrv"),
        ]

    def test_a_com_spec_naming_a_member_the_files_own_interface_lacks_is_a_verdict(self, tmp_path: Path) -> None:
        """The file tier too: under the port's own interface path, a member the declaration lacks."""
        text = _export(tmp_path).replace(
            "/PortInterfaces/ISig/a</DATA-ELEMENT-REF>", "/PortInterfaces/ISig/zz</DATA-ELEMENT-REF>"
        )

        missing, _written = _withheld(_save(tmp_path, text), _target(tmp_path))

        assert [(m.referrer, m.reference_kind, m.missing_member, m.missing_name) for m in missing] == [
            ("port 'rSig' NONQUEUED-RECEIVER-COM-SPEC", "data_element", "zz", "ISig")
        ]
        assert all("/" not in m.line() for m in missing)

    def test_a_com_spec_naming_a_member_under_another_path_is_a_line(self, tmp_path: Path) -> None:
        """Never bound by its leaf: a member under another interface path is a line, no verdict."""
        text = _export(tmp_path).replace(
            "/PortInterfaces/ISig/a</DATA-ELEMENT-REF>", "/PortInterfaces/IOther/a</DATA-ELEMENT-REF>"
        )
        target = _target(tmp_path)

        report = import_component(_save(tmp_path, text), str(target))

        assert report.missing_references == []
        assert any(
            line.startswith("NONQUEUED-RECEIVER-COM-SPEC /ComponentTypes/Comp/rSig/REQUIRED-COM-SPECS")
            and "names no data element of the port's interface" in line
            for line in report.not_carried_lines
        )
        assert _written_swc(target)["runnables"]

    def test_import_models_report_names_the_same_line(self, tmp_path: Path) -> None:
        """``import model``'s report on the same file names the line ``import component`` prints."""
        text = _export(tmp_path).replace(
            "</NONQUEUED-RECEIVER-COM-SPEC>",
            "<HANDLE-DATA-STATUS>true</HANDLE-DATA-STATUS></NONQUEUED-RECEIVER-COM-SPEC>",
        )
        source = _save(tmp_path, text)
        project = init_project(tmp_path / "whole")

        whole = run_import(source, str(project / "swcraft.yaml"))
        component = import_component(source, str(_target(tmp_path)))

        added = [line for line in whole.skip_lines if line.startswith("HANDLE-DATA-STATUS ")]
        assert added == component.not_carried_lines


# `import model` is unchanged


class TestImportModelOfTheSameFileIsUnchanged:
    """``import model`` passes its own, unfiltered policy: the stub, its listed member and the kept read."""

    def test_the_whole_export_import_still_stubs_the_interface_and_lists_its_member(self, tmp_path: Path) -> None:
        # Outside the writer's root packages, where the stub rule admits a stub.
        text = _without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig").replace(
            "/PortInterfaces/ISig", "/Elsewhere/ISig"
        )
        source = _save(tmp_path, text)
        project = init_project(tmp_path / "whole")

        report = run_import(source, str(project / "swcraft.yaml"))

        assert [(stub.target, stub.dest) for stub in report.external_interface_stubs] == [
            ("/Elsewhere/ISig", "SENDER-RECEIVER-INTERFACE")
        ]
        name = report.external_interface_stubs[0].name
        stub = yaml.safe_load((project / "model" / "interfaces" / f"{name}.yaml").read_text(encoding="utf-8"))
        assert stub["data_elements"] == ["a"]
        assert stub["external_ref"] == "/Elsewhere/ISig"
        written = next((project / "model" / "swc").rglob("Comp.yaml"))
        swc = yaml.safe_load(written.read_text(encoding="utf-8"))
        assert {"name": "rSig", "direction": "require", "interface": name} in swc["ports"]
        assert swc["runnables"][0]["reads"] == [{"port": "rSig", "element": "a"}]


def test_a_client_server_and_a_sender_receiver_target_interface_are_both_declared_answers() -> None:
    """The second tier admits both declared interface kinds."""
    sig = SRInterface.model_validate(_sig("a"))
    srv = CSInterface.model_validate(_srv("Op"))

    assert target_declared_interface("/X/ISig", {"ISig": sig, "ISrv": srv}, {_SR}) is sig
    assert target_declared_interface("/X/ISrv", {"ISig": sig, "ISrv": srv}, {"CLIENT-SERVER-INTERFACE"}) is srv


# The `--update` overlay: the project's behaviour kept, asked over the overlay


def _export_of(tmp_path: Path, component: dict, *interfaces: dict) -> str:
    """SWCraft's own export of *component* over ``ISig``/``ISrv`` and *interfaces*, with a set the target lacks.

    The set ``/Elsewhere/Gone`` is one ``mapping_set`` verdict, so the incoming behaviour is always withheld.
    """
    source = init_project(tmp_path / "overlay-source")
    model_root = source / "model"
    _seed_types(model_root)
    for interface in (_sig("a"), _srv("Op"), *interfaces):
        write_model_element(model_root, interface)
    swc = model_root / "swc" / "body" / "Comp.yaml"
    swc.parent.mkdir(parents=True, exist_ok=True)
    swc.write_text(yaml.safe_dump(component, sort_keys=False), encoding="utf-8", newline="\n")
    arxml = tmp_path / "Overlay.arxml"
    export_component(str(model_root), "Comp", str(arxml))
    text = _with_mapping_set_reference(arxml.read_text(encoding="utf-8")).replace("/Elsewhere/Maps", "/Elsewhere/Gone")
    return _save(tmp_path, text, "overlay.arxml")


def _incoming(*extra_ports: dict) -> dict:
    """The file's component: ``rSig`` and ``pSrv`` and *extra_ports*, one timing runnable (withheld anyway)."""
    return {
        "name": "Comp",
        "type": "application",
        "ports": [
            {"name": "rSig", "direction": "require", "interface": "ISig"},
            {"name": "pSrv", "direction": "provide", "interface": "ISrv"},
            *extra_ports,
        ],
        "runnables": [{"name": "Tick", "events": [{"type": "timing", "period": 0.02}]}],
        "handle_termination_and_restart": "can_be_terminated",
    }


def _held_target(tmp_path: Path, held: dict, *interfaces: dict) -> tuple[Path, Path]:
    """A project holding ``ISig``, ``ISrv``, *interfaces*, the set ``Maps`` and the component *held*."""
    target = _target(tmp_path, _sig("a"), _srv("Op"), *interfaces, name="held")
    write_model_element(target, {"name": "Maps", "maps": []}, layer="mapping_sets")
    path = target / "swc" / "components" / "Comp.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(held, sort_keys=False), encoding="utf-8", newline="\n")
    return target, path


def _held(*extra_ports: dict, extra_runnables: tuple[dict, ...] = (), **fields: object) -> dict:
    """The project's ``Comp``: :func:`_component`'s behaviour, the set ``Maps``, *extra_ports* and *fields*."""
    held = _component()
    held["ports"] = [*held["ports"], *extra_ports]
    held["runnables"] = [*held["runnables"], *extra_runnables]
    held["data_type_mappings"] = ["Maps"]
    held.update(fields)
    return held


def _read_comp(model_root: Path) -> SWComponent:
    return next(swc for swc in read_swc_model(str(model_root)) if swc.name == "Comp")


def _broken_update(source: str, target: Path) -> list[str]:
    """Import *source* with ``--update`` into *target*: refused as a conflict, nothing written; its lines."""
    before = files_of(target)
    with pytest.raises(ComponentImportConflictError) as caught:
        import_component(source, str(target), update=True)
    assert files_of(target) == before, "a refused update wrote something"
    # No storage path: a line names layers and elements (a mode reference is model data, an AUTOSAR path).
    assert all(str(target) not in line and ".yaml" not in line for line in caught.value.conflicts)
    return caught.value.conflicts


_OLD_READ = {"name": "rOld", "direction": "require", "interface": "ISig"}
_READS_OLD = {"name": "K", "events": [{"type": "init"}], "reads": [{"port": "rOld", "element": "a"}]}


class TestTheUpdateKeepsTheProjectsBehaviour:
    """``--update`` over a component the target holds, with the file's behaviour withheld.

    The overlay -- the file's ports, interfaces and description over the project's own behaviour --
    is written once the validator's predicates, asked in memory, find no kept reference it breaks;
    a broken one is a conflict naming it, and nothing is written.
    """

    def test_a_withheld_update_keeps_the_behaviour_and_the_carried_fields(self, tmp_path: Path) -> None:
        held = _held(_OLD_READ, memory={"ram": 64}, asil="B", package="Body")
        held["ports"][0]["measures"] = {"a": "End-to-end protection."}
        target, _path = _held_target(tmp_path, held)
        source = _export_of(tmp_path, _incoming())

        report = import_component(source, str(target), update=True)

        assert report.swc_action == "updated"
        assert report.missing_references
        kept = _read_comp(target)
        expected = SWComponent.model_validate(held)
        assert kept.runnables == expected.runnables
        assert kept.data_type_mappings == ["Maps"]
        assert kept.behavior_name == expected.behavior_name
        assert kept.handle_termination_and_restart == "no_support", "the project's own value, not the file's"
        assert kept.memory == {"ram": 64}
        assert kept.asil == "B"
        assert kept.package == "Body"
        assert next(port for port in kept.ports if port.name == "rSig").measures == {"a": "End-to-end protection."}
        assert sorted(port.name for port in kept.ports) == ["pSrv", "rSig"], "the file's ports: rOld is gone"
        rendered = report.render()
        assert report.behaviour_kept
        count = len(report.missing_references)
        assert _block(rendered)[0] == f"{MISSING_REFERENCES_HEADING} ({count}; {MISSING_REFERENCES_KEPT_TAIL}):"
        assert "Update diff preview:" in rendered

    def test_a_removed_port_a_kept_operation_event_uses_is_refused(self, tmp_path: Path) -> None:
        serves = {"name": "K", "events": [{"type": "operation", "port": "pOld", "operation": "Op"}]}
        held = _held({"name": "pOld", "direction": "provide", "interface": "ISrv"}, extra_runnables=(serves,))
        target, _path = _held_target(tmp_path, held)

        conflicts = _broken_update(_export_of(tmp_path, _incoming()), target)

        assert len(conflicts) == 1
        assert "keeps the project's own behaviour" in conflicts[0]
        assert "declares no port 'pOld'" in conflicts[0]

    def test_a_direction_change_is_refused(self, tmp_path: Path) -> None:
        target, _path = _held_target(tmp_path, _held(_OLD_READ, extra_runnables=(_READS_OLD,)))
        flipped = {**_OLD_READ, "direction": "provide"}

        conflicts = _broken_update(_export_of(tmp_path, _incoming(flipped)), target)

        assert len(conflicts) == 1
        assert "reads port 'rOld', which is a provide port" in conflicts[0]

    def test_an_interface_change_without_the_member_is_refused(self, tmp_path: Path) -> None:
        other = {**_sig("b"), "name": "IOther"}
        target, _path = _held_target(tmp_path, _held(_OLD_READ, extra_runnables=(_READS_OLD,)))
        moved = {**_OLD_READ, "interface": "IOther"}

        conflicts = _broken_update(_export_of(tmp_path, _incoming(moved), other), target)

        assert len(conflicts) == 1
        assert "element 'a' of port 'rOld', which interface 'IOther' does not declare" in conflicts[0]

    def test_a_removed_port_a_kept_disabled_mode_names_is_refused(self, tmp_path: Path) -> None:
        mode_port = {"name": "rMode", "direction": "require", "interface": "IMode"}
        held = _held(mode_port)
        held["runnables"][0]["events"][0]["disabled_modes"] = [
            {"port": "rMode", "mode_group_ref": "/Ext/IMode/mg", "mode_ref": "/Ext/Modes/OFF"}
        ]
        target, _path = _held_target(tmp_path, held, _target_stub("IMode", "/Ext/IMode", dest="MODE-SWITCH-INTERFACE"))

        conflicts = _broken_update(_export_of(tmp_path, _incoming()), target)

        assert len(conflicts) == 1
        assert "on port 'rMode'" in conflicts[0]

    def test_a_kept_by_value_read_the_overlay_makes_queued_is_refused(self, tmp_path: Path) -> None:
        by_value = {**_READS_OLD, "reads": [{"port": "rOld", "element": "a", "by": "value"}]}
        target, _path = _held_target(tmp_path, _held(_OLD_READ, extra_runnables=(by_value,)))
        queued = {**_sig("a", queued=frozenset({"a"})), "name": "IQueued"}
        moved = {**_OLD_READ, "interface": "IQueued", "com_specs": [{"data_element": "a", "queue_length": 2}]}

        conflicts = _broken_update(_export_of(tmp_path, _incoming(moved), queued), target)

        assert len(conflicts) == 1
        assert "reads queued data element 'a' through port 'rOld' by value" in conflicts[0]

    def test_a_finding_the_project_already_has_does_not_refuse_the_update(self, tmp_path: Path) -> None:
        """The predicates judge what the update would break; the project's own defect is check-model's."""
        broken = {"name": "K", "events": [{"type": "init"}], "reads": [{"port": "rGone", "element": "a"}]}
        target, _path = _held_target(tmp_path, _held(_OLD_READ, extra_runnables=(broken,)))

        report = import_component(_export_of(tmp_path, _incoming()), str(target), update=True)

        assert report.swc_action == "updated"
        assert [runnable.name for runnable in _read_comp(target).runnables] == ["R", "S", "K"]

    def test_a_complete_incoming_behaviour_replaces_the_projects_as_today(self, tmp_path: Path) -> None:
        target, _path = _held_target(tmp_path, _held(_OLD_READ))
        text = Path(_export_of(tmp_path, _incoming())).read_text(encoding="utf-8")
        complete = _save(tmp_path, text.replace("/Elsewhere/Gone", "/Elsewhere/Maps"), "complete.arxml")

        report = import_component(complete, str(target), update=True)

        assert report.missing_references == []
        written = _read_comp(target)
        assert [runnable.name for runnable in written.runnables] == ["Tick"]
        assert written.data_type_mappings == ["Maps"]
        assert written.handle_termination_and_restart == "can_be_terminated"

    def test_a_reimport_without_update_and_an_identical_descriptive_part_is_unchanged(self, tmp_path: Path) -> None:
        target, path = _held_target(tmp_path, _held())
        before = path.read_bytes()

        report = import_component(_export_of(tmp_path, _incoming()), str(target))

        assert report.swc_action == "unchanged"
        assert report.missing_references
        assert path.read_bytes() == before
        assert report.behaviour_kept
        rendered = report.render()
        count = len(report.missing_references)
        assert _block(rendered)[0] == f"{MISSING_REFERENCES_HEADING} ({count}; {MISSING_REFERENCES_UNCHANGED_TAIL}):"
        assert "written over it" not in rendered

    def test_a_reimport_without_update_and_a_different_descriptive_part_is_a_conflict(self, tmp_path: Path) -> None:
        target, _path = _held_target(tmp_path, _held(_OLD_READ))
        before = files_of(target)

        with pytest.raises(ComponentImportConflictError) as caught:
            import_component(_export_of(tmp_path, _incoming()), str(target))

        # The whole line: it is the ordinary divergence line, not the overlay's.
        assert caught.value.conflicts == [
            "SWC 'Comp' already exists in the target project and diverges (pass --update to replace the "
            "component's own definition; the comparison already ignores fields the exported ARXML does not "
            "carry: a description the incoming export leaves empty, a value type's unit, data element init "
            "values the incoming export does not declare, a termination and restart handling it does not "
            "state, port order, memory budgets, declared safety integrity levels, declared measures, a port's "
            "observed interface, and the runnable access lists reads/writes/calls -- this is a real, "
            "representable difference)"
        ]
        assert files_of(target) == before


# The report block and the stub marks


def _block(rendered: str) -> tuple[str, list[str]]:
    """The *References the project does not have* heading of *rendered* and the lines under it."""
    lines = rendered.splitlines()
    heading = next(line for line in lines if line.startswith(MISSING_REFERENCES_HEADING))
    block = []
    for line in lines[lines.index(heading) + 1 :]:
        if not line.startswith("  - "):
            break
        block.append(line)
    return heading, block


def _assert_path_free(lines: list[str], *roots: Path) -> None:
    """No ARXML path, no storage path, no file name: the path-free gate's twin for the block."""
    for line in lines:
        assert "/" not in line and "\\" not in line, line
        assert ".yaml" not in line and ".arxml" not in line, line
        for root in roots:
            assert str(root) not in line and root.as_posix() not in line, line


class TestTheReportBlockRendersEachKind:
    """One line per verdict, by layer and element, path-free, under one sentence naming what is withheld."""

    def _rendered_block(self, source: str, target: Path) -> tuple[str, list[str], list[MissingReference]]:
        planned = plan_component_import(source, str(target))
        heading, block = _block(planned.report.render())
        return heading, block, list(planned.missing_references)

    def test_a_data_element_line(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _without_com_specs(_without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig")))
        target = _target(tmp_path, _sig("other"))

        heading, block, missing = self._rendered_block(source, target)

        assert heading == f"{MISSING_REFERENCES_HEADING} (1; {MISSING_REFERENCES_HEADING_TAIL}):"
        assert block == [
            "  - swc 'Comp' runnable 'R' read through port 'rSig' names the data element 'a', "
            "which the interfaces element 'ISig' does not declare"
        ]
        assert block == [f"  - {record.line()}" for record in missing]
        _assert_path_free(block, tmp_path)

    def test_an_operation_line(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _without_com_specs(_without(_export(tmp_path), "CLIENT-SERVER-INTERFACE", "ISrv")))
        target = _target(tmp_path, _srv("Other"))

        _heading, block, _missing = self._rendered_block(source, target)

        assert block == [
            "  - swc 'Comp' runnable 'S' operation event through port 'pSrv' names the operation 'Op', "
            "which the interfaces element 'ISrv' does not declare"
        ]
        _assert_path_free(block, tmp_path)

    def test_a_mode_group_line(self, tmp_path: Path) -> None:
        target = _target(tmp_path, _target_stub("IModeStub", "/Elsewhere/IMode", dest="MODE-SWITCH-INTERFACE"))

        _heading, block, missing = self._rendered_block(_save(tmp_path, _with_disabled_mode(_export(tmp_path))), target)

        assert [record.reference_kind for record in missing] == ["mode_group"]
        assert block == [f"  - {missing[0].line()}"]
        assert block[0].startswith("  - swc 'Comp' runnable 'R' ")
        assert block[0].endswith(
            "names the mode group 'mg' of the external interface stub 'IModeStub', "
            "which neither the file nor the project declares"
        )
        _assert_path_free(block, tmp_path)

    def test_a_mapping_set_line(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _with_mapping_set_reference(_export(tmp_path)))

        _heading, block, _missing = self._rendered_block(source, _target(tmp_path))

        assert block == [
            "  - swc 'Comp' internal behaviour names the mapping_sets element 'Maps', "
            "which neither the file nor the project declares"
        ]
        _assert_path_free(block, tmp_path)

    def test_several_verdicts_print_one_line_each_in_the_records_order(self, tmp_path: Path) -> None:
        text = _with_mapping_set_reference(
            _without_com_specs(_without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig"))
        )
        target = _target(tmp_path, _sig("other"))

        heading, block, missing = self._rendered_block(_save(tmp_path, text), target)

        assert heading.startswith(f"{MISSING_REFERENCES_HEADING} (2; ")
        assert block == [f"  - {record.line()}" for record in missing]
        assert [record.reference_kind for record in missing] == ["data_element", "mapping_set"]
        _assert_path_free(block, tmp_path)

    def test_the_block_sits_right_under_the_component_line(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _without_com_specs(_without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig")))

        lines = plan_component_import(source, str(_target(tmp_path, _sig("other")))).report.render().splitlines()

        assert lines[lines.index("SWC Comp: created") + 1].startswith(MISSING_REFERENCES_HEADING)

    def test_no_verdict_is_a_mark_nowhere(self, tmp_path: Path) -> None:
        """A complete component prints neither the block nor a stub mark."""
        source = _save(tmp_path, _without_com_specs(_without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig")))

        rendered = plan_component_import(source, str(_target(tmp_path, _sig("a")))).report.render()

        assert MISSING_REFERENCES_HEADING not in rendered
        assert STUB_INTERFACE_MARK not in rendered


class TestTheStubMarks:
    """A minted stub is marked in the CREATED list, a reused one in the REUSED list, nothing else is."""

    def test_a_minted_stub_is_marked_in_the_created_list(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _elsewhere(_export(tmp_path), "ISig"))
        report = plan_component_import(source, str(_target(tmp_path))).report
        stub = stub_name("ISig", 0, {"ISrv"})[0]

        lines = report.render().splitlines()

        assert report.stub_interfaces == [stub]
        created = lines[lines.index("CREATED 2 interface(s)") + 1 :][:2]
        assert sorted(created) == sorted([f"  - {stub} {STUB_INTERFACE_MARK}", "  - ISrv"])
        assert "REUSED 0 interface(s)" in lines

    def test_a_reused_stub_is_marked_in_the_reused_list(self, tmp_path: Path) -> None:
        target = _target(tmp_path, _target_stub("ISigStub", "/Elsewhere/ISig", "a"))

        report = plan_component_import(_save(tmp_path, _elsewhere(_export(tmp_path), "ISig")), str(target)).report

        lines = report.render().splitlines()
        assert report.stub_interfaces == ["ISigStub"]
        assert lines[lines.index("CREATED 1 interface(s)") + 1] == "  - ISrv"
        assert lines[lines.index("REUSED 1 interface(s)") + 1] == f"  - ISigStub {STUB_INTERFACE_MARK}"

    def test_a_declared_interface_the_target_answers_is_not_marked(self, tmp_path: Path) -> None:
        """The target's declared interface answering a port is never a stub, so never marked."""
        source = _save(tmp_path, _without_com_specs(_without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig")))

        report = plan_component_import(source, str(_target(tmp_path, _sig("other")))).report

        assert report.stub_interfaces == []
        assert STUB_INTERFACE_MARK not in report.render()


class TestThePreviewAndTheDryRunShowTheBlock:
    """The block reaches every surface the report does: the plan's preview, ``--dry-run`` and ``--report``."""

    def _source_and_target(self, tmp_path: Path) -> tuple[str, Path]:
        text = _with_mapping_set_reference(_elsewhere(_without_com_specs(_export(tmp_path)), "ISig"))
        return _save(tmp_path, text), _target(tmp_path)

    def test_the_preview_and_the_dry_run_render_the_same_block_and_write_nothing(self, tmp_path: Path) -> None:
        source, target = self._source_and_target(tmp_path)
        before = files_of(target)

        preview = plan_component_import(source, str(target), dry_run=True).report.render()
        dry = import_component(source, str(target), dry_run=True)

        assert files_of(target) == before, "a dry run wrote something"
        assert dry.render() == preview
        assert "(DRY RUN -- no files were written)" in preview
        heading, block = _block(preview)
        assert heading.startswith(f"{MISSING_REFERENCES_HEADING} (2; ")
        assert block == [f"  - {line}" for line in dry.missing_references]
        assert any(STUB_INTERFACE_MARK in line for line in preview.splitlines())
        _assert_path_free(block, tmp_path)

    def test_the_cli_dry_run_report_file_carries_the_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source, target = self._source_and_target(tmp_path)
        before = files_of(target)
        report_file = tmp_path / "import-report.txt"
        argv = [
            "swcraft",
            "import",
            "component",
            "--source",
            source,
            "--project",
            str(target.parent / "swcraft.yaml"),
            "--dry-run",
            "--report",
            str(report_file),
        ]
        monkeypatch.setattr("sys.argv", argv)
        capsys.readouterr()  # the scaffolding's own output

        main()

        printed = capsys.readouterr().out
        written = report_file.read_text(encoding="utf-8")
        assert files_of(target) == before, "a dry run wrote into the project"
        assert written.rstrip("\n") == printed.rstrip("\n")
        heading, block = _block(written)
        assert MISSING_REFERENCES_HEADING_TAIL in heading
        assert len(block) == 2
        _assert_path_free(block, tmp_path)


# Edges of the late pass, the file tier and the overlay


def _export_text(tmp_path: Path, component: dict, name: str) -> str:
    """SWCraft's own export of *component* (named ``Comp``) over ``ISig`` (``a``) and ``ISrv`` (``Op``), as text."""
    source = init_project(tmp_path / f"{name}-source")
    model_root = source / "model"
    _seed_types(model_root)
    write_model_element(model_root, _sig("a"))
    write_model_element(model_root, _srv("Op"))
    swc = model_root / "swc" / "body" / "Comp.yaml"
    swc.parent.mkdir(parents=True, exist_ok=True)
    swc.write_text(yaml.safe_dump(component, sort_keys=False), encoding="utf-8", newline="\n")
    arxml = tmp_path / f"{name}.arxml"
    export_component(str(model_root), "Comp", str(arxml))
    return arxml.read_text(encoding="utf-8")


#: A component that writes through a provide port and calls through a client port.
_WRITER = {
    "name": "Comp",
    "type": "application",
    "ports": [
        {"name": "pSig", "direction": "provide", "interface": "ISig"},
        {"name": "rSrv", "direction": "require", "interface": "ISrv"},
    ],
    "runnables": [
        {
            "name": "W",
            "events": [{"type": "timing", "period": 0.01}],
            "writes": [{"port": "pSig", "element": "a"}],
            "calls": [{"port": "rSrv", "operation": "Op"}],
        }
    ],
    "handle_termination_and_restart": "no_support",
}


def _read_target_ref(text: str, ref: str) -> str:
    """*text* with the read's ``TARGET-DATA-PROTOTYPE-REF`` (``/PortInterfaces/ISig/a``) replaced by *ref*."""
    old = '<TARGET-DATA-PROTOTYPE-REF DEST="VARIABLE-DATA-PROTOTYPE">/PortInterfaces/ISig/a<'
    assert text.count(old) == 1
    return text.replace(old, f'<TARGET-DATA-PROTOTYPE-REF DEST="VARIABLE-DATA-PROTOTYPE">{ref}<')


class TestTheFileTierMemberMissIsAVerdict:
    """A member the file's own interface at the exact path lacks: a missing reference, as on the target tier.

    A reference under **another** interface path keeps its refusal: a member is never bound by its leaf.
    """

    def test_a_read_of_a_data_element_the_files_interface_lacks(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _read_target_ref(_export(tmp_path), "/PortInterfaces/ISig/zz"))
        target = _target(tmp_path)

        missing, written = _withheld(source, target)

        assert missing == [
            MissingReference(
                referrer_layer="swc",
                referrer_name="Comp",
                referrer="runnable 'R' read through port 'rSig'",
                missing_member="zz",
                reference_kind="data_element",
                missing_layer="interfaces",
                missing_name="ISig",
            )
        ]
        assert missing[0].line() == (
            "swc 'Comp' runnable 'R' read through port 'rSig' names the data element 'zz', "
            "which the interfaces element 'ISig' does not declare"
        )
        assert {"name": "rSig", "direction": "require", "interface": "ISig"} in written["ports"]
        assert _interface_file(target, "ISig")["data_elements"] == [{"name": "a", "implementation_type": "u8"}]
        _link_and_check(target)

    def test_an_operation_event_naming_an_operation_the_files_interface_lacks(self, tmp_path: Path) -> None:
        old = '<TARGET-PROVIDED-OPERATION-REF DEST="CLIENT-SERVER-OPERATION">/PortInterfaces/ISrv/Op<'
        text = _export(tmp_path)
        assert text.count(old) == 1
        text = text.replace(old, old.replace("/ISrv/Op<", "/ISrv/Zz<"))
        target = _target(tmp_path)

        missing, _written = _withheld(_save(tmp_path, text), target)

        assert [(m.referrer, m.reference_kind, m.missing_member, m.missing_name) for m in missing] == [
            ("runnable 'S' operation event through port 'pSrv'", "operation", "Zz", "ISrv")
        ]
        _link_and_check(target)

    def test_a_member_under_another_interface_path_still_refuses(self, tmp_path: Path) -> None:
        source = _save(tmp_path, _read_target_ref(_export(tmp_path), "/PortInterfaces/ISrv/a"))

        refusal = _refusal(source, _target(tmp_path))

        assert any(
            "unresolved reference /PortInterfaces/ISrv/a (not a data element of port 'rSig's interface 'ISig')" in line
            for line in refusal.skip_lines
        )
        assert refusal.missing_references == []


class TestWritesAndCallsThroughATargetResolvedPort:
    """The late pass's two other access lists: a write through a provide port, a call through a client port."""

    def _source(self, tmp_path: Path, *, com_specs: bool = True) -> str:
        text = _without(
            _without(_export_text(tmp_path, _WRITER, "writer"), "SENDER-RECEIVER-INTERFACE", "ISig"),
            "CLIENT-SERVER-INTERFACE",
            "ISrv",
        )
        return _save(tmp_path, text if com_specs else _without_com_specs(text))

    def test_a_write_and_a_call_the_targets_interfaces_declare_are_admitted(self, tmp_path: Path) -> None:
        target = _target(tmp_path, _sig("a"), _srv("Op"))

        report = import_component(self._source(tmp_path), str(target))

        assert report.swc_action == "created"
        assert report.missing_references == []
        (runnable,) = _written_swc(target)["runnables"]
        assert runnable["writes"] == [{"port": "pSig", "element": "a"}]
        assert runnable["calls"] == [{"port": "rSrv", "operation": "Op"}]
        _link_and_check(target)

    def test_a_write_and_a_call_the_targets_interfaces_lack_are_verdicts(self, tmp_path: Path) -> None:
        target = _target(tmp_path, _sig("other"), _srv("Other"))

        missing, _written = _withheld(self._source(tmp_path, com_specs=False), target)

        assert sorted(m.line() for m in missing) == [
            "swc 'Comp' runnable 'W' call through port 'rSrv' names the operation 'Op', "
            "which the interfaces element 'ISrv' does not declare",
            "swc 'Comp' runnable 'W' write through port 'pSig' names the data element 'a', "
            "which the interfaces element 'ISig' does not declare",
        ]


def _as_provide_port(text: str) -> str:
    """*text* with ``rSig`` turned into a provide port, its read's port reference with it."""
    edited, count = re.subn(
        r"<R-PORT-PROTOTYPE>(\s*<SHORT-NAME>rSig</SHORT-NAME>\s*)<REQUIRED-INTERFACE-TREF(.*?)"
        r"</REQUIRED-INTERFACE-TREF>(\s*)</R-PORT-PROTOTYPE>",
        r"<P-PORT-PROTOTYPE>\1<PROVIDED-INTERFACE-TREF\2</PROVIDED-INTERFACE-TREF>\3</P-PORT-PROTOTYPE>",
        text,
        flags=re.DOTALL,
    )
    assert count == 1
    old = '<PORT-PROTOTYPE-REF DEST="R-PORT-PROTOTYPE">/ComponentTypes/Comp/rSig<'
    assert edited.count(old) == 1
    return edited.replace(old, old.replace("R-PORT-PROTOTYPE", "P-PORT-PROTOTYPE"))


class TestTheLateBranchesKeepTheFileTiersLines:
    """``admit_held_records`` through a port the target answered: the three checks that refuse as the file tier does."""

    def _refused(self, tmp_path: Path, text: str) -> LossyComponentImportError:
        """*text* (no com-specs) without ``ISig``, imported into a target declaring ``ISig`` with ``a``: refused."""
        return _refusal(
            _save(tmp_path, _without(text, "SENDER-RECEIVER-INTERFACE", "ISig")), _target(tmp_path, _sig("a"))
        )

    def test_a_read_through_a_provide_port_is_refused_with_its_line(self, tmp_path: Path) -> None:
        refusal = self._refused(tmp_path, _as_provide_port(_without_com_specs(_export(tmp_path))))

        assert any(
            "reads access through provide port 'rSig' (a reads entry needs a require port)" in line
            for line in refusal.skip_lines
        )
        assert refusal.missing_references == []

    def test_a_member_reference_that_is_no_path_is_refused_with_its_line(self, tmp_path: Path) -> None:
        refusal = self._refused(tmp_path, _read_target_ref(_without_com_specs(_export(tmp_path)), "a"))

        assert any("unresolved reference a (not an AUTOSAR path)" in line for line in refusal.skip_lines)

    def test_a_member_under_another_interface_path_is_never_bound_by_its_leaf(self, tmp_path: Path) -> None:
        refusal = self._refused(tmp_path, _read_target_ref(_without_com_specs(_export(tmp_path)), "/Other/ISig/a"))

        assert any(
            "unresolved reference /Other/ISig/a (not a data element of port 'rSig's interface 'ISig')" in line
            for line in refusal.skip_lines
        )
        assert refusal.missing_references == []


class TestALateImplicitReadIsJudgedAgainstItsComSpec:
    """A held implicit read admitted through a port the target answered.

    ``[constr_1103]`` refuses an implicit read of an element whose port's com-spec states
    ``ENABLE-UPDATE`` true. Guards an ``admit_held_records`` that named the queued case alone.
    """

    def test_the_line_is_named_and_the_import_refused(self, tmp_path: Path) -> None:
        text = _export(tmp_path, read_by="implicit")
        assert text.count("<ENABLE-UPDATE>false</ENABLE-UPDATE>") == 1
        text = _without(
            text.replace("<ENABLE-UPDATE>false</ENABLE-UPDATE>", "<ENABLE-UPDATE>true</ENABLE-UPDATE>"),
            "SENDER-RECEIVER-INTERFACE",
            "ISig",
        )

        refusal = _refusal(_save(tmp_path, text), _target(tmp_path, _sig("a")))

        assert any(
            "R_rSig_a (R4.2.2 [constr_1103]): DATA-READ-ACCESS reads data element 'a' through port 'rSig' "
            "implicitly, and the port's com-spec states ENABLE-UPDATE true for it" in line
            for line in refusal.skip_lines
        ), refusal.skip_lines

    def test_without_the_flag_the_read_is_admitted(self, tmp_path: Path) -> None:
        text = _without(_export(tmp_path, read_by="implicit"), "SENDER-RECEIVER-INTERFACE", "ISig")
        target = _target(tmp_path, _sig("a"))

        import_component(_save(tmp_path, text), str(target))

        (runnable,) = [r for r in _written_swc(target)["runnables"] if r["name"] == "R"]
        assert runnable["reads"] == [{"port": "rSig", "element": "a", "by": "implicit"}]


class TestALateRefusalNamesItsVerdictsToo:
    def test_a_blocking_line_and_a_verdict_are_both_in_the_refusal(self, tmp_path: Path) -> None:
        """A queued element without its queue length refuses; the other port's verdict is named beside it."""
        text = _without(
            _without(_export(tmp_path), "SENDER-RECEIVER-INTERFACE", "ISig"), "CLIENT-SERVER-INTERFACE", "ISrv"
        )
        target = _target(tmp_path, _sig("a", queued=frozenset({"a"})), _srv("Other"))

        refusal = _refusal(_save(tmp_path, text), target)

        assert any(
            "no QUEUED-RECEIVER-COM-SPEC is defined for queued data element 'a'" in line for line in refusal.skip_lines
        )
        assert refusal.missing_references, "the operation verdicts travel with the refusal"
        assert {(m.reference_kind, m.missing_member, m.missing_name) for m in refusal.missing_references} == {
            ("operation", "Op", "ISrv")
        }
        assert [line for line in refusal.skip_lines if "names the operation 'Op'" in line] == [
            m.line() for m in refusal.missing_references
        ]
        assert f"References the project does not have ({len(refusal.missing_references)}): " in str(refusal)


class TestTheReuseKeepsTheStubRulesSafeguards:
    """The stub rule's safeguards hold on the reuse path as on the minting one."""

    def test_two_referrers_disagreeing_on_dest_are_refused_with_a_target_stub_present(self, tmp_path: Path) -> None:
        text = _with_ports(
            _without_com_specs(_export(tmp_path)),
            _port("rOne", "/Elsewhere/IX"),
            _port("rTwo", "/Elsewhere/IX", dest="CLIENT-SERVER-INTERFACE"),
        )

        refusal = _refusal(_save(tmp_path, text), _target(tmp_path, _target_stub("IXStub", "/Elsewhere/IX")))

        refused = {
            port
            for port in ("rOne", "rTwo")
            for line in refusal.skip_lines
            if f"/ComponentTypes/Comp/{port}" in line and "unresolved reference /Elsewhere/IX" in line
        }
        assert refused == {"rOne", "rTwo"}

    def test_a_target_stub_of_another_dest_is_not_reused_and_the_import_is_refused(self, tmp_path: Path) -> None:
        """A target stub of the port's path with another ``DEST`` is not reused, and minting one is a conflict.

        One path cannot be a sender-receiver and a client-server interface at once: a divergent
        shared definition aborts and nothing is written.
        """
        target = _target(tmp_path, _target_stub("IXStub", "/Elsewhere/IX", "Op", dest="CLIENT-SERVER-INTERFACE"))
        before = (target / "interfaces" / "IXStub.yaml").read_bytes()
        text = _with_ports(_without_com_specs(_export(tmp_path)), _port("rOne", "/Elsewhere/IX"))

        with pytest.raises(ComponentImportConflictError) as exc_info:
            import_component(_save(tmp_path, text), str(target))

        lines = [line for line in exc_info.value.conflicts if "'IXStub'" in line]
        assert len(lines) == 1, exc_info.value.conflicts
        assert "/Elsewhere/IX" in lines[0]
        assert (target / "interfaces" / "IXStub.yaml").read_bytes() == before
        assert not list((target / "interfaces").glob("IX_ext*.yaml"))

    def test_of_two_target_stubs_of_one_external_ref_the_first_by_name_is_reused(self, tmp_path: Path) -> None:
        target = _target(
            tmp_path, _target_stub("ZStub", "/Elsewhere/ISig", "a"), _target_stub("AStub", "/Elsewhere/ISig", "a")
        )

        report = import_component(_save(tmp_path, _elsewhere(_export(tmp_path), "ISig")), str(target))

        assert report.reused_interfaces == ["AStub"]
        assert {"name": "rSig", "direction": "require", "interface": "AStub"} in _written_swc(target)["ports"]

    def test_the_first_by_name_whose_dest_the_reference_carries_is_reused(self, tmp_path: Path) -> None:
        target = _target(
            tmp_path,
            _target_stub("AStub", "/Elsewhere/ISig", dest="CLIENT-SERVER-INTERFACE"),
            _target_stub("ZStub", "/Elsewhere/ISig", "a"),
        )

        report = import_component(_save(tmp_path, _elsewhere(_export(tmp_path), "ISig")), str(target))

        assert report.reused_interfaces == ["ZStub"]


def _external_comp() -> dict:
    """The project's ``Comp`` as ``import model`` leaves a component it could not resolve: a stub."""
    return {
        "name": "Comp",
        "type": "application",
        "external": True,
        "external_ref": "/Foreign/Comp",
        "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
    }


class TestTheUpdateOverlayEdges:
    """The ``--update`` overlay at its edges."""

    def test_an_update_over_an_external_stub_writes_the_files_component_withheld(self, tmp_path: Path) -> None:
        """A stub has no behaviour to keep: the file's two behaviour fields are written, as on a create.

        Guards an overlay that copied the stub's absent ``handle_termination_and_restart``.
        """
        target = _target(tmp_path, _sig("a"), _srv("Op"))
        path = target / "swc" / "components" / "Comp.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(_external_comp(), sort_keys=False), encoding="utf-8", newline="\n")

        report = import_component(_export_of(tmp_path, _incoming()), str(target), update=True)

        assert report.swc_action == "updated"
        assert report.missing_references
        written = _read_comp(target)
        assert not written.external
        assert written.handle_termination_and_restart == "can_be_terminated", "the file's value"
        assert written.runnables == []
        assert written.data_type_mappings == []
        assert sorted(port.name for port in written.ports) == ["pSrv", "rSig"]
        # The overlay did not run, so the heading is a create's.
        assert not report.behaviour_kept
        count = len(report.missing_references)
        heading = _block(report.render())[0]
        assert heading == f"{MISSING_REFERENCES_HEADING} ({count}; {MISSING_REFERENCES_HEADING_TAIL}):"
        _link_and_check(target)

    def test_a_runnable_event_collision_the_project_already_has_does_not_refuse(self, tmp_path: Path) -> None:
        """``rule:name.runnable-event-collision`` the kept behaviour already carries is the project's own."""
        colliding = {"name": "TMT_R", "events": [{"type": "init"}]}
        target, _path = _held_target(tmp_path, _held(extra_runnables=(colliding,)))

        report = import_component(_export_of(tmp_path, _incoming(_OLD_READ)), str(target), update=True)

        assert report.swc_action == "updated"
        assert [runnable.name for runnable in _read_comp(target).runnables] == ["R", "S", "TMT_R"]

    def test_an_update_with_an_identical_descriptive_part_is_unchanged(self, tmp_path: Path) -> None:
        target, path = _held_target(tmp_path, _held())
        before = path.read_bytes()

        report = import_component(_export_of(tmp_path, _incoming()), str(target), update=True)

        assert report.swc_action == "unchanged"
        assert path.read_bytes() == before
        assert report.missing_references
        rendered = report.render()
        count = len(report.missing_references)
        assert _block(rendered)[0] == f"{MISSING_REFERENCES_HEADING} ({count}; {MISSING_REFERENCES_UNCHANGED_TAIL}):"
        assert "written over it" not in rendered

    def test_a_file_interface_diverging_from_the_projects_of_that_name_is_refused(self, tmp_path: Path) -> None:
        """The kept read names ``ISig``'s ``a``; the file's ``ISig`` lacks it: a divergent shared definition."""
        target, _path = _held_target(tmp_path, _held(_OLD_READ, extra_runnables=(_READS_OLD,)))
        before = files_of(target)

        with pytest.raises(ComponentImportConflictError) as caught:
            import_component(_export_of(tmp_path, _incoming(), _sig("b")), str(target), update=True)

        assert any("ISig" in conflict for conflict in caught.value.conflicts)
        assert files_of(target) == before

    def test_an_incoming_port_taking_the_kept_behaviours_name_is_refused(self, tmp_path: Path) -> None:
        """The kept ``behavior_name`` and an incoming port share the component's name space."""
        target, _path = _held_target(tmp_path, _held(behavior_name="Beh"))
        before = files_of(target)

        with pytest.raises(ReservedNameError) as caught:
            import_component(
                _export_of(tmp_path, _incoming({"name": "Beh", "direction": "require", "interface": "ISig"})),
                str(target),
                update=True,
            )

        assert "'Beh'" in str(caught.value)
        assert files_of(target) == before


def test_a_missing_reference_of_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown missing-reference kind 'port'"):
        MissingReference(
            referrer_layer="swc",
            referrer_name="Comp",
            referrer="port 'p'",
            missing_member="m",
            reference_kind="port",
            missing_layer="interfaces",
            missing_name="I",
        )

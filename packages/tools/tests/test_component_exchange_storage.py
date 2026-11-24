"""The SWC storage record is what ``component_exchange/`` composes SWC paths with.

Neither direction restates the SWC layer's storage rule (``swc/<group>/<name>.yaml``) inline.
``_EXPORT_TEMP_SWC_GROUP`` (``"component"``) and ``_IMPORT_TARGET_SWC_GROUP`` (``"components"``)
are not knowledge of that rule and must not be deleted as leftovers: ``group`` is placement metadata
no ARXML carries, so an export *invents* one for its temp tree and an import *chooses* one.

Three claims: **parity** with paths composed from the literal :data:`PRE_MIGRATION_SWC_STORAGE`,
so the oracle is not the code under test; **the record is really read**, both directions moving
with a perturbed ``storage``; **no literal survives**, an AST scan shown to bite on a planted literal path.

**What this module does not reach.** The AST gate is scoped to the SWC layer: the import's target
scan reads every other scanned layer's directory from the registry too, and
:class:`TestTheTargetScanReadsEachLayersDirectoryFromTheRegistry` relocates ``interfaces`` and
``implementation_types`` alone; no AST gate holds those layers. The gate sees only the scanned
modules and only *literal* spellings: a directory name computed at run time is invisible to it, as
to ``test_generic_core_gate.py``'s check (b).
"""

from __future__ import annotations

import ast
import copy
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import yaml
from project_support import init_project
from swcraft_tools.component_exchange import divergence as divergence_module
from swcraft_tools.component_exchange import export as export_module
from swcraft_tools.component_exchange import import_ as import_module
from swcraft_tools.component_exchange import resulting_behaviour as resulting_behaviour_module
from swcraft_tools.component_exchange import target_carry as target_carry_module
from swcraft_tools.component_exchange import target_project as target_project_module
from swcraft_tools.component_exchange.closure import compute_closure
from swcraft_tools.component_exchange.export import (
    _EXPORT_TEMP_SWC_GROUP,
    _materialize_closure,
    export_all_components,
    export_component,
)
from swcraft_tools.component_exchange.import_ import (
    _IMPORT_TARGET_SWC_GROUP,
    AmbiguousTargetProjectError,
    import_component,
    plan_component_import,
)
from swcraft_tools.contract import load_binding, load_contract
from swcraft_tools.model_registry import build_model_registry, production_registry, use_registry
from test_component_export import _build_model, _dump, _short_names
from test_component_import import _export_target

#: The SWC layer's storage rule, ``{model_root}/swc/{group}/{name}.yaml``, as a literal on purpose:
#: a parity assertion derived from the registry the code reads would agree with any registry.
PRE_MIGRATION_SWC_STORAGE = ("swc", ".yaml")

#: The two placement values, pinned so that renaming one is a test change and
#: not a silent relocation of every component an import creates.
PRE_MIGRATION_EXPORT_GROUP = "component"
PRE_MIGRATION_IMPORT_GROUP = "components"


def _pre_migration_swc_path(root: Path, group: str, name: str) -> Path:
    directory, suffix = PRE_MIGRATION_SWC_STORAGE
    return root / directory / group / f"{name}{suffix}"


# Parity with the literal SWC storage oracle


class TestBothDirectionsWriteThePreMigrationPaths:
    def test_the_export_materializes_the_swc_where_it_always_did(self, tmp_path: Path) -> None:
        """``_materialize_closure`` directly: ``export_component`` deletes its temp tree."""
        model_root = _build_model(tmp_path)
        closure = compute_closure(model_root, "TargetSwc")
        materialized = tmp_path / "temp-tree"

        _materialize_closure(closure, materialized)

        expected = _pre_migration_swc_path(materialized, PRE_MIGRATION_EXPORT_GROUP, "TargetSwc")
        assert expected.is_file(), sorted(p.as_posix() for p in materialized.rglob("*.yaml"))
        written = sorted(path.relative_to(materialized).as_posix() for path in materialized.rglob("*.yaml"))
        assert "swc/component/TargetSwc.yaml" in written
        assert [entry for entry in written if entry.startswith("swc/")] == ["swc/component/TargetSwc.yaml"]

    def test_the_batch_export_still_finds_every_swc_in_the_layer(self, tmp_path: Path) -> None:
        """``export_all_components``' listing: a directory read from the wrong place is an empty batch."""
        model_root = _build_model(tmp_path)
        reports = export_all_components(model_root, str(tmp_path / "out"))
        assert sorted(report.swc for report in reports) == ["OtherSwc", "TargetSwc"]

    def test_the_import_plans_the_swc_where_it_always_did(self, tmp_path: Path) -> None:
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"

        plan = plan_component_import(str(arxml), str(model_root))

        (swc_target,) = [target for target in plan.targets if target.layer == "swc"]
        assert Path(swc_target.path) == _pre_migration_swc_path(model_root, PRE_MIGRATION_IMPORT_GROUP, "TargetSwc")
        assert swc_target.placement == {"group": PRE_MIGRATION_IMPORT_GROUP}

    def test_the_import_writes_the_swc_where_it_always_did(self, tmp_path: Path) -> None:
        """The applied path, not only the planned one — ``_atomic_apply`` renames onto it."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"

        report = import_component(str(arxml), str(model_root))

        expected = _pre_migration_swc_path(model_root, PRE_MIGRATION_IMPORT_GROUP, "TargetSwc")
        assert expected.is_file()
        assert str(expected) in report.written_paths

    def test_an_update_still_replaces_the_recorded_file_and_never_the_composed_one(self, tmp_path: Path) -> None:
        """An update writes the exact file the target scan recorded: composing a path would relocate it."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        elsewhere = _pre_migration_swc_path(model_root, "hand_placed", "TargetSwc")
        import_component(str(arxml), str(model_root))
        created = _pre_migration_swc_path(model_root, PRE_MIGRATION_IMPORT_GROUP, "TargetSwc")
        elsewhere.parent.mkdir(parents=True, exist_ok=True)
        elsewhere.write_text(created.read_text(encoding="utf-8"), encoding="utf-8")
        created.unlink()

        plan = plan_component_import(str(arxml), str(model_root), update=True)

        assert [Path(target.path) for target in plan.targets if target.layer == "swc"] in ([], [elsewhere])
        assert not created.exists()
        assert elsewhere.is_file()


# The record is really read: the perturbation seam


def _contract_with_swc_storage(**changes: Any) -> dict[str, Any]:
    """The shipped contract with the SWC layer's ``storage`` block amended."""
    contract = copy.deepcopy(load_contract())
    layer = next(entry for entry in contract["layers"] if entry["name"] == "swc")
    layer["storage"].update(changes)
    return contract


@pytest.fixture
def relocated_swc_layer():
    """A registry whose SWC layer lives in ``units/`` and stores ``.yml`` files.

    Both halves move: a module that read the directory and kept ``f"{name}.yaml"`` would pass a
    directory-only perturbation.
    """
    return build_model_registry(
        contract=_contract_with_swc_storage(directory="units", fileSuffix=".yml"),
        binding=load_binding(),
    )


class TestBothDirectionsFollowAPerturbedStorageRecord:
    def test_the_export_materializes_under_the_perturbed_record(self, tmp_path: Path, relocated_swc_layer) -> None:
        model_root = _build_model(tmp_path)
        closure = compute_closure(model_root, "TargetSwc")
        materialized = tmp_path / "temp-tree"

        with use_registry(relocated_swc_layer):
            _materialize_closure(closure, materialized)

        assert (materialized / "units" / _EXPORT_TEMP_SWC_GROUP / "TargetSwc.yml").is_file()
        assert not (materialized / "swc").exists()

    def test_the_import_plans_under_the_perturbed_record(self, tmp_path: Path, relocated_swc_layer) -> None:
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"

        with use_registry(relocated_swc_layer):
            plan = plan_component_import(str(arxml), str(model_root))

        (swc_target,) = [target for target in plan.targets if target.layer == "swc"]
        assert Path(swc_target.path) == model_root / "units" / _IMPORT_TARGET_SWC_GROUP / "TargetSwc.yml"
        assert swc_target.placement == {"group": _IMPORT_TARGET_SWC_GROUP}

    def test_the_registry_is_resolved_per_call_and_not_captured(self, tmp_path: Path, relocated_swc_layer) -> None:
        """Leaving the block restores the shipped record, so nothing bound it at import time."""
        model_root = _build_model(tmp_path)
        closure = compute_closure(model_root, "TargetSwc")
        with use_registry(relocated_swc_layer):
            _materialize_closure(closure, tmp_path / "inside")
        _materialize_closure(closure, tmp_path / "outside")

        assert (tmp_path / "inside" / "units" / _EXPORT_TEMP_SWC_GROUP / "TargetSwc.yml").is_file()
        assert _pre_migration_swc_path(tmp_path / "outside", PRE_MIGRATION_EXPORT_GROUP, "TargetSwc").is_file()


def _registry_with_layer_moved(layer: str, directory: str) -> Any:
    """The production registry with *layer* stored in *directory* instead.

    The registry refuses a ``retiredStorage`` entry naming a directory no layer stores in, so the
    move rewrites the layer's old directory there too.
    """
    contract = copy.deepcopy(load_contract())
    entry = next(entry for entry in contract["layers"] if entry["name"] == layer)
    shipped = entry["storage"]["directory"]
    entry["storage"]["directory"] = directory
    for retired in contract.get("retiredStorage", ()):
        retired["movedTo"] = [directory if target == shipped else target for target in retired["movedTo"]]
    return build_model_registry(contract=contract, binding=load_binding())


def _declare_probe_dup_bases(model_root: Path) -> None:
    """The two base types the duplicate value types below rest on, in the (unmoved) base layer."""
    for side, size in (("A", 8), ("B", 16)):
        _dump(
            model_root / "data_types" / "base" / f"ProbeDup{side}_base.yaml",
            {"name": f"ProbeDup{side}_base", "kind": "base_type", "size": size, "encoding": "NONE"},
        )


class TestTheTargetScanReadsEachLayersDirectoryFromTheRegistry:
    """The import's target scan finds a layer stored in a directory of two segments.

    An ambiguity scan joining ``root / "interfaces"`` or ``root / "data_types"`` as literals would
    scan the wrong place once a layer's directory moves.
    """

    @pytest.mark.parametrize(
        ("layer", "directory", "documents"),
        [
            pytest.param(
                "implementation_types",
                "types/declared",
                # Value types: the kind the implementation layer's directory admits.
                [
                    {"name": "Dup_T", "kind": "value_type", "base": "ProbeDupA_base"},
                    {"name": "Dup_T", "kind": "value_type", "base": "ProbeDupB_base"},
                ],
                id="implementation_types",
            ),
            pytest.param(
                "interfaces",
                "ports/declared",
                [
                    {"name": "IDup", "type": "sender_receiver", "data_elements": []},
                    {"name": "IDup", "type": "sender_receiver", "data_elements": []},
                ],
                id="interfaces",
            ),
        ],
    )
    def test_a_duplicate_in_a_two_segment_directory_refuses_the_import(
        self, tmp_path: Path, layer: str, directory: str, documents: list[dict[str, Any]]
    ) -> None:
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        relocated = _registry_with_layer_moved(layer, directory)
        _declare_probe_dup_bases(model_root)
        for index, document in enumerate(documents):
            _dump(model_root / directory / f"Dup{index}.yaml", document)

        with use_registry(relocated), pytest.raises(AmbiguousTargetProjectError) as refused:
            plan_component_import(str(arxml), str(model_root))

        shared = str(refused.value)
        assert f"{directory}/Dup0.yaml" in shared and f"{directory}/Dup1.yaml" in shared, shared

    def test_the_shipped_directory_is_not_scanned_once_the_layer_moved(self, tmp_path: Path) -> None:
        """The literal's own place: a duplicate there is no longer the moved layer's, so nothing refuses on it."""
        _, arxml = _export_target(tmp_path)
        project = init_project(tmp_path / "proj")
        model_root = project / "model"
        relocated = _registry_with_layer_moved("implementation_types", "types/declared")
        _declare_probe_dup_bases(model_root)
        for index, side in enumerate(("A", "B")):
            _dump(
                model_root / "data_types" / "implementation" / f"Dup{index}.yaml",
                {"name": "Dup_T", "kind": "value_type", "base": f"ProbeDup{side}_base"},
            )

        with use_registry(relocated):
            plan = plan_component_import(str(arxml), str(model_root))

        assert {target.layer for target in plan.targets} >= {"implementation_types", "swc"}
        assert all(
            "types/declared/" in Path(target.path).as_posix()
            for target in plan.targets
            if target.layer == "implementation_types"
        )


# The two constants are values, not rules


class TestTheTwoGroupConstantsAreValuesNotRules:
    """The values themselves and their deliberate singular/plural difference.

    `test_model_contract_coverage.py`'s `_exchange_group_constants` holds the other half: both
    assignments exist and are used.
    """

    def test_the_values_are_the_pre_migration_ones(self) -> None:
        assert _EXPORT_TEMP_SWC_GROUP == PRE_MIGRATION_EXPORT_GROUP
        assert _IMPORT_TARGET_SWC_GROUP == PRE_MIGRATION_IMPORT_GROUP

    def test_neither_value_is_a_layer_directory(self) -> None:
        """A group is placement, so a value colliding with a layer name would read as one."""
        directories = {layer.storage.directory for layer in production_registry().layers}
        assert _EXPORT_TEMP_SWC_GROUP not in directories
        assert _IMPORT_TARGET_SWC_GROUP not in directories


# No SWC directory literal survives in either module

_EXCHANGE_MODULES = (export_module, import_module)

#: Every module the literal scan reads. Only the two above read the SWC layer's record
#: (`storage("swc")`), so the positive half below is asked of them alone.
_SCANNED_MODULES = (
    *_EXCHANGE_MODULES,
    divergence_module,
    resulting_behaviour_module,
    target_carry_module,
    target_project_module,
)


def _composed_directory_literals(tree: ast.Module, directory: str) -> list[int]:
    """Every line where *directory* is a string literal used to build a path.

    Two shapes: an operand of a ``/`` join, and an argument to ``Path(...)`` or ``.joinpath(...)``.
    Classified by position, not spelling, because both modules legitimately pass ``"swc"`` as a
    *layer name* (``registry.storage("swc")``, ``PlannedTarget(layer="swc")``).
    """
    found: list[int] = []
    for node in ast.walk(tree):
        operands: list[ast.expr] = []
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            operands = [node.left, node.right]
        elif isinstance(node, ast.Call):
            func = node.func
            named = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if named in {"Path", "joinpath"}:
                operands = list(node.args)
        for operand in operands:
            if isinstance(operand, ast.Constant) and operand.value == directory:
                found.append(operand.lineno)
    return sorted(found)


class TestNeitherModuleComposesTheSwcPathFromALiteral:
    def test_the_swc_directory_literal_is_gone_from_both_modules(self) -> None:
        directory = production_registry().storage("swc").directory
        offenders = {
            module.__name__: _composed_directory_literals(
                ast.parse(Path(module.__file__).read_text(encoding="utf-8")), directory
            )
            for module in _SCANNED_MODULES
        }
        assert {name: lines for name, lines in offenders.items() if lines} == {}

    def test_the_detector_bites_on_the_pre_migration_expression(self) -> None:
        """The gate's own reachability: a detector that matched nothing would report clean forever."""
        probe = ast.parse(
            'swc_path = root / "swc" / group / f"{name}.yaml"\n'
            'swc_dir = root / "swc"\n'
            'scanned = Path("swc")\n'
            'layer_name = registry.storage("swc")\n'
            'target = PlannedTarget(layer="swc")\n'
        )
        assert _composed_directory_literals(probe, "swc") == [1, 2, 3]

    def test_both_modules_read_the_record_instead(self) -> None:
        """The positive half: absence alone is satisfiable by deleting the code."""
        for module in _EXCHANGE_MODULES:
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            storage_calls = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "storage"
                and [arg for arg in node.args if isinstance(arg, ast.Constant) and arg.value == "swc"]
            ]
            assert storage_calls, module.__name__


# The temp tree the export materializes decides its file names the same way


#: The sub-package ``_model_with_case_only_data_types`` places ``float32`` in.
_TWIN_PACKAGE = "Legacy"


def _placed_short_names(arxml_path: Path) -> tuple[set[str], set[str]]:
    """The element ``SHORT-NAME``s of ``/DataTypes/Implementation`` itself, and of its sub-package ``_TWIN_PACKAGE``."""
    root = ET.parse(arxml_path).getroot()

    def local(element: ET.Element) -> str:
        return element.tag.split("}")[-1]

    def short_name(element: ET.Element) -> str | None:
        return next((child.text for child in element if local(child) == "SHORT-NAME"), None)

    def elements_of(package: ET.Element) -> set[str]:
        return {
            name
            for holder in package
            if local(holder) == "ELEMENTS"
            for element in holder
            if (name := short_name(element)) is not None
        }

    data_types = next(
        element for element in root.iter() if local(element) == "AR-PACKAGE" and short_name(element) == "DataTypes"
    )
    home = next(
        element
        for holder in data_types
        if local(holder) == "AR-PACKAGES"
        for element in holder
        if short_name(element) == "Implementation"
    )
    sub_package = next(
        element
        for holder in home
        if local(holder) == "AR-PACKAGES"
        for element in holder
        if short_name(element) == _TWIN_PACKAGE
    )
    return elements_of(home), elements_of(sub_package)


def _model_with_case_only_data_types(tmp_path: Path, *, twin_suffixed: bool) -> str:
    """:func:`_build_model` plus a ``Float32`` twin of its ``float32``, both inside the closure.

    A case-insensitive filesystem stores the pair as one file, so the second gets ``~1``;
    *twin_suffixed* swaps which one carries it.
    """
    model_root = Path(_build_model(tmp_path))
    types = model_root / "data_types" / "implementation"
    (types / "float32.yaml").unlink()
    plain, twin = ("float32.yaml", "Float32~1.yaml") if twin_suffixed else ("float32~1.yaml", "Float32.yaml")
    # The pair shares one name space ignoring case (`[constr_2508]`), so `float32` is PLACED in a
    # sub-package, as `import model` records it for such a pair.
    _dump(types / plain, {"name": "float32", "kind": "value_type", "base": "float32_base", "package": _TWIN_PACKAGE})
    _dump(types / twin, {"name": "Float32", "kind": "value_type", "base": "float32_base"})
    interface = model_root / "interfaces" / "ISpeed.yaml"
    document = yaml.safe_load(interface.read_text(encoding="utf-8"))
    # Both spellings must be IN the closure, and no composite member reaches `float32`.
    document["data_elements"].append({"name": "big", "implementation_type": "Float32"})
    document["data_elements"].append({"name": "small", "implementation_type": "float32"})
    _dump(interface, document)
    return str(model_root)


class TestACaseOnlyPairExportsLikeAnyOtherModel:
    """The temp tree stores a case-only pair through ``free_storage_path`` like every other writer.

    Guards a hand-joined file name, with which on APFS and NTFS one of ``Float32`` / ``float32``
    replaced the other and the export reported the survivor *undeclared*.
    """

    def test_the_single_component_export_carries_both_spellings(self, tmp_path: Path) -> None:
        model_root = _model_with_case_only_data_types(tmp_path, twin_suffixed=True)
        output = tmp_path / "out" / "TargetSwc.arxml"

        report = export_component(model_root, "TargetSwc", str(output))

        assert report.status == "ok"
        assert {"Float32", "float32"} <= _short_names(output)
        in_root, in_sub_package = _placed_short_names(output)
        assert "Float32" in in_root and "float32" not in in_root
        assert in_sub_package == {"float32"}
        assert "~" not in output.read_text(encoding="utf-8"), "a temp-tree file name reached the emitted ARXML"

    def test_the_batch_export_carries_both_spellings_and_fails_no_component(self, tmp_path: Path) -> None:
        """The batch is where the defect was disguised: a per-SWC ``status="failed"`` row."""
        model_root = _model_with_case_only_data_types(tmp_path, twin_suffixed=True)
        out_dir = tmp_path / "batch"

        reports = export_all_components(model_root, str(out_dir))

        assert [(report.swc, report.status, report.error) for report in reports] == [
            ("OtherSwc", "ok", None),
            ("TargetSwc", "ok", None),
        ]
        assert {"Float32", "float32"} <= _short_names(out_dir / "TargetSwc.arxml")
        in_root, in_sub_package = _placed_short_names(out_dir / "TargetSwc.arxml")
        assert "Float32" in in_root and in_sub_package == {"float32"}

    def test_the_emitted_bytes_do_not_depend_on_which_twin_holds_the_suffix(self, tmp_path: Path) -> None:
        """The suffix is the file's: the same model stored two ways exports the same bytes."""
        first = export_component(
            _model_with_case_only_data_types(tmp_path / "a", twin_suffixed=True),
            "TargetSwc",
            str(tmp_path / "a.arxml"),
        )
        second = export_component(
            _model_with_case_only_data_types(tmp_path / "b", twin_suffixed=False),
            "TargetSwc",
            str(tmp_path / "b.arxml"),
        )

        assert first.status == second.status == "ok"
        assert (tmp_path / "a.arxml").read_bytes() == (tmp_path / "b.arxml").read_bytes()

    def test_the_twin_adds_its_own_element_and_nothing_else(self, tmp_path: Path) -> None:
        """Against the same model without the twin: the delta is the pair, their data elements and the sub-package.

        Nothing is missing, the half the defect broke: the export lost whichever spelling was written first.
        """
        with_twin = tmp_path / "twin.arxml"
        without_twin = tmp_path / "plain.arxml"
        export_component(
            _model_with_case_only_data_types(tmp_path / "a", twin_suffixed=True), "TargetSwc", str(with_twin)
        )
        export_component(_build_model(tmp_path / "b"), "TargetSwc", str(without_twin))

        assert _short_names(with_twin) - _short_names(without_twin) == {
            "Float32",
            "big",
            "float32",
            "small",
            _TWIN_PACKAGE,
        }
        assert _short_names(without_twin) - _short_names(with_twin) == set()

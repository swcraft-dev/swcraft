"""A model on a retired layout is refused when it is read (contract ``8.0.0``).

The contract's ``retiredStorage`` table names ``data_types`` (moved to ``data_types/application``,
``data_types/implementation`` and ``data_types/base``) and ``mapping_sets`` (moved to
``data_types/mapping_sets``). ``model_state.refuse_retired_storage`` lists each one level deep and
raises ``RetiredStorageError``, a ``ValueError`` naming the directory, the count of files and where
they belong, all model-root-relative.

Covered: the registry's reading of the table and its refusals; the refusal per retired place and
both at once; what is NOT refused (an empty retired directory, the new layout, a file one level
deeper, another suffix); the sentence and its path-freedom; the gate and the read-only commands
exiting non-zero; a git revision on the retired layout; the editing protocol per request shape.
The MCP surface is ``packages/mcp/tests/test_no_tool_reply_carries_a_path.py``'s
``model-on-a-retired-layout`` mode.

What this does not reach: a retired directory is recognised by the element files directly in it
and by nothing else, as ``model_state.py::refuse_retired_storage``'s docstring says; the tests
below hold each half.
"""

from __future__ import annotations

import copy
import shutil
import sys
from pathlib import Path

import pytest
from conftest import run_captured
from scratch_git import git as _git
from swcraft_tools.contract import load_binding, load_contract
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.model_registry import ModelRegistryError, RetiredStorage, build_model_registry, production_registry
from swcraft_tools.model_state import RetiredStorageError, load_model_state, refuse_retired_storage
from swcraft_tools.reader import read_layer

REPO_ROOT = Path(__file__).resolve().parents[3]
BASIC = REPO_ROOT / "examples" / "basic"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A copy of ``examples/basic`` on the current layout."""
    target = tmp_path / "basic"
    shutil.copytree(BASIC, target, ignore=shutil.ignore_patterns("dist"))
    return target


def _model(project: Path) -> Path:
    return project / "model"


def _retire_implementation_types(model: Path) -> int:
    """Move every implementation type file back into ``data_types/``; the count moved."""
    moved = 0
    for path in sorted((model / "data_types" / "implementation").glob("*.yaml"), key=Path.as_posix):
        path.rename(model / "data_types" / path.name)
        moved += 1
    return moved


def _retire_a_mapping_set(model: Path) -> None:
    (model / "mapping_sets").mkdir()
    (model / "mapping_sets" / "SyntheticSet.yaml").write_text(
        "name: SyntheticSet\nmaps: []\n", encoding="utf-8", newline="\n"
    )


class TestTheContractTable:
    def test_the_shipped_contract_retires_two_directories(self) -> None:
        assert production_registry().retired_storage == (
            RetiredStorage(
                directory="data_types",
                since="8.0.0",
                moved_to=("data_types/application", "data_types/implementation", "data_types/base"),
                file_suffix=".yaml",
            ),
            RetiredStorage(
                directory="mapping_sets", since="8.0.0", moved_to=("data_types/mapping_sets",), file_suffix=".yaml"
            ),
        )

    def test_a_retired_directory_that_a_layer_stores_in_is_refused(self) -> None:
        contract = copy.deepcopy(load_contract())
        contract["retiredStorage"][0]["directory"] = "interfaces"
        with pytest.raises(ModelRegistryError, match="is the storage directory of layer 'interfaces'"):
            build_model_registry(contract=contract, binding=load_binding())

    def test_a_retired_directory_moved_to_no_layers_directory_is_refused(self) -> None:
        contract = copy.deepcopy(load_contract())
        contract["retiredStorage"][1]["movedTo"] = ["data_types/nowhere"]
        with pytest.raises(ModelRegistryError, match="no layer stores its elements there"):
            build_model_registry(contract=contract, binding=load_binding())

    def test_a_retired_directory_moved_to_layers_of_two_suffixes_is_refused(self) -> None:
        """The refusal could not say which files are element files, so the table is refused."""
        contract = copy.deepcopy(load_contract())
        for layer in contract["layers"]:
            if layer["name"] == "base_types":
                layer["storage"]["fileSuffix"] = ".yml"
        with pytest.raises(
            ModelRegistryError, match=r"moved to layers whose file suffixes differ: \['\.yaml', '\.yml'\]"
        ):
            build_model_registry(contract=contract, binding=load_binding())

    def test_a_retired_directory_that_is_a_case_variant_of_a_layers_directory_is_refused(self) -> None:
        """One directory on a case-insensitive filesystem: the layer would read the files the table refuses.

        The contract would contradict itself, so the registry is not built.
        """
        contract = copy.deepcopy(load_contract())
        contract["retiredStorage"][1]["directory"] = "Interfaces"
        with pytest.raises(ModelRegistryError) as caught:
            build_model_registry(contract=contract, binding=load_binding())
        message = str(caught.value)
        assert message == (
            "retired directory 'Interfaces' and 'interfaces', the directory of layer 'interfaces', differ only in "
            "case: a case-insensitive filesystem holds them as one directory"
        )
        assert str(REPO_ROOT) not in message
        assert "/model/" not in message

    def test_a_retired_directory_inside_a_nesting_layers_tree_is_refused(self) -> None:
        """``swc/legacy`` lies inside ``swc``: its files would be read as the group ``legacy`` and refused.

        The same prefix rule as two layer directories. A retired
        directory that *holds* a layer's directory is the shipped shape
        (``data_types`` holds ``data_types/application``) and is admitted —
        ``test_the_shipped_contract_retires_two_directories``.
        """
        contract = copy.deepcopy(load_contract())
        contract["retiredStorage"][1]["directory"] = "swc/legacy"
        with pytest.raises(ModelRegistryError) as caught:
            build_model_registry(contract=contract, binding=load_binding())
        message = str(caught.value)
        assert message == (
            "retired directory 'swc/legacy' lies inside 'swc', the directory of layer 'swc': its files would be "
            "read by that layer and refused"
        )
        assert str(REPO_ROOT) not in message
        assert "/model/" not in message

    def test_a_retired_directory_that_holds_layers_directories_is_admitted(self) -> None:
        """The legal direction of the prefix rule, on the shipped shape.

        The retired ``data_types`` holds ``data_types/application`` and its two
        siblings: the refusal reads only the element files directly in
        ``data_types``, each layer only those in its own directory, so nothing is
        read and refused at once, and the registry is built.
        """
        registry = build_model_registry(contract=copy.deepcopy(load_contract()), binding=load_binding())
        retired = registry.retired_storage[0]
        held = [directory for directory in retired.moved_to if directory.startswith(f"{retired.directory}/")]

        assert retired.directory == "data_types"
        assert "data_types/application" in held
        assert held == list(retired.moved_to)
        assert {registry.storage(name).directory for name in registry.layer_names} >= set(held)

    def test_a_contract_without_the_table_retires_nothing(self) -> None:
        contract = copy.deepcopy(load_contract())
        del contract["retiredStorage"]
        assert build_model_registry(contract=contract, binding=load_binding()).retired_storage == ()


class TestTheRefusal:
    def test_the_current_layout_is_read(self, project: Path) -> None:
        refuse_retired_storage(_model(project))
        assert load_model_state(_model(project))["implementation_types"]

    def test_a_file_in_the_retired_data_types_directory_is_refused_by_every_read(self, project: Path) -> None:
        moved = _retire_implementation_types(_model(project))
        assert moved == 12
        expected = (
            "The model keeps its elements in a retired layout: 12 element files in 'data_types', a directory "
            "contract 8.0.0 retired; they belong in 'data_types/application', 'data_types/implementation' or "
            "'data_types/base'. Move each file into the directory of its layer; nothing converts a model on the "
            "old layout."
        )
        for read in (
            lambda: refuse_retired_storage(_model(project)),
            lambda: load_model_state(_model(project)),
            # Any layer's read refuses, not only the layers the files belong in.
            lambda: read_layer(str(_model(project)), "hardware"),
        ):
            with pytest.raises(RetiredStorageError) as refused:
                read()
            assert str(refused.value) == expected

    def test_each_entry_point_that_reads_layers_refuses_before_any_file_is_parsed(self, project: Path) -> None:
        """The refusal is asked once per read at each entry point, not inside the per-layer loader.

        ``load_layer``, the reader's SWC-with-placement read and the working-tree
        snapshot each refuse with the live sentence; a parse error in the layer
        read (an unparseable SWC file) is not reached first.
        """
        from swcraft_tools.model_state import load_layer
        from swcraft_tools.reader import read_swc_model_with_placement
        from swcraft_tools.snapshot import load_snapshot

        model = _model(project)
        _retire_implementation_types(model)
        broken = model / "swc" / "ProbeBroken.yaml"
        broken.write_text("name: [unclosed\n", encoding="utf-8", newline="\n")
        for read in (
            lambda: load_layer(model, "swc"),
            lambda: read_swc_model_with_placement(str(model)),
            lambda: load_snapshot(str(model)),
        ):
            with pytest.raises(RetiredStorageError) as refused:
                read()
            assert str(refused.value).startswith("The model keeps its elements in a retired layout: 12 element files")

    def test_a_file_in_the_retired_mapping_sets_directory_is_refused(self, project: Path) -> None:
        _retire_a_mapping_set(_model(project))
        with pytest.raises(RetiredStorageError) as refused:
            refuse_retired_storage(_model(project))
        assert str(refused.value) == (
            "The model keeps its elements in a retired layout: 1 element file in 'mapping_sets', a directory "
            "contract 8.0.0 retired; it belongs in 'data_types/mapping_sets'. Move each file into the directory of "
            "its layer; nothing converts a model on the old layout."
        )

    def test_both_retired_directories_are_named_in_the_contracts_order(self, project: Path) -> None:
        _retire_a_mapping_set(_model(project))
        _retire_implementation_types(_model(project))
        with pytest.raises(RetiredStorageError) as refused:
            refuse_retired_storage(_model(project))
        message = str(refused.value)
        assert message.index("12 element files in 'data_types'") < message.index("1 element file in 'mapping_sets'")
        assert [retired.directory for retired, _ in refused.value.held] == ["data_types", "mapping_sets"]

    def test_the_sentence_names_no_location_above_the_model_root(self, project: Path) -> None:
        _retire_implementation_types(_model(project))
        with pytest.raises(RetiredStorageError) as refused:
            load_model_state(_model(project))
        message = str(refused.value)
        for location in (str(project), project.as_posix(), str(project.resolve()), str(Path.home())):
            assert location not in message
        assert isinstance(refused.value, ValueError)

    def test_an_empty_retired_directory_is_not_an_error(self, project: Path) -> None:
        (_model(project) / "mapping_sets").mkdir()
        refuse_retired_storage(_model(project))
        assert load_model_state(_model(project))["mapping_sets"] == []

    def test_a_file_deeper_or_of_another_suffix_is_not_an_element_file(self, project: Path) -> None:
        model = _model(project)
        (model / "mapping_sets" / "nested").mkdir(parents=True)
        (model / "mapping_sets" / "nested" / "Deeper.yaml").write_text("name: Deeper\n", encoding="utf-8")
        (model / "data_types" / "notes.txt").write_text("not a model file\n", encoding="utf-8")
        (model / "data_types" / "README.YAML.d").mkdir()
        refuse_retired_storage(model)

    def test_a_directory_named_like_an_element_file_is_not_one(self, project: Path) -> None:
        (_model(project) / "data_types" / "Folder.yaml").mkdir()
        refuse_retired_storage(_model(project))


class TestTheCommandLine:
    """The gate and every read-only command exit non-zero, naming the refusal.

    ``check-model`` is the gate; ``diff``, ``impact``, ``list``, ``metrics``, ``query`` and
    ``resource-report`` are the read-only six, which exit 0 on any project they
    could read and 1 on one they cannot — and a retired layout is one they
    cannot. ``diff`` compares ``HEAD`` with the working tree, so the project is
    a git repository whose ``HEAD`` is on the current layout: the working tree
    is the side refused, and the sentence names no revision.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["check-model"],
            ["diff"],
            ["impact", "uint8"],
            ["list"],
            ["metrics"],
            ["metrics", "--since-head"],
            ["query", "interfaces"],
            ["resource-report"],
        ],
        ids=["check-model", "diff", "impact", "list", "metrics", "metrics-since-head", "query", "resource-report"],
    )
    def test_a_retired_layout_is_a_project_the_command_cannot_read(self, project: Path, argv: list[str]) -> None:
        _commit_all(project, "the current layout")
        _retire_implementation_types(_model(project))
        completed = run_captured(
            [sys.executable, "-m", "swcraft_tools.cli", *argv, "--project", project / "swcraft.yaml"], cwd=project
        )
        assert completed.returncode == 1, completed.stdout + completed.stderr
        assert "The model keeps its elements in a retired layout: 12 element files in 'data_types'" in (
            completed.stderr
        )
        assert "in revision" not in completed.stderr
        assert str(project) not in completed.stderr


def _commit_all(project: Path, message: str) -> None:
    """Make *project* a git repository (once) and commit its whole tree."""
    if not (project / ".git").exists():
        _git("init", cwd=project)
    _git("add", "-A", cwd=project)
    _git("commit", "-q", "--allow-empty", "-m", message, cwd=project)


def _restore_implementation_types(model: Path) -> None:
    """Move every element file of the retired ``data_types/`` back into ``data_types/implementation/``."""
    for path in sorted((model / "data_types").glob("*.yaml"), key=Path.as_posix):
        path.rename(model / "data_types" / "implementation" / path.name)


class TestARevisionOnTheRetiredLayout:
    """A git revision on the old layout is refused like the working tree, and the sentence names it.

    Nothing reads an old-layout revision — ``swcraft diff`` across the commit that moved a model
    is refused on the old side — and the refusal names the revision as the
    caller passed it, never the temporary directory it was extracted into. The
    live model root's sentence is unchanged (``TestTheCommandLine`` and
    ``TestTheRefusal`` hold it).
    """

    def test_load_model_state_of_a_retired_revision_names_the_revision(self, project: Path) -> None:
        _retire_implementation_types(_model(project))
        _commit_all(project, "the old layout")
        _restore_implementation_types(_model(project))

        assert load_model_state(_model(project))["implementation_types"]
        with pytest.raises(RetiredStorageError) as refused:
            load_model_state(_model(project), rev="HEAD")
        assert str(refused.value) == (
            "The model keeps its elements in a retired layout in revision 'HEAD': 12 element files in 'data_types', "
            "a directory contract 8.0.0 retired; they belong in 'data_types/application', "
            "'data_types/implementation' or 'data_types/base'. Move each file into the directory of its layer; "
            "nothing converts a model on the old layout."
        )
        assert refused.value.revision == "HEAD"

    def test_diff_across_the_move_is_refused_on_the_old_side_naming_its_revision(self, project: Path) -> None:
        _retire_implementation_types(_model(project))
        _commit_all(project, "the old layout")
        _restore_implementation_types(_model(project))
        _commit_all(project, "the move to the new layout")

        completed = run_captured(
            [sys.executable, "-m", "swcraft_tools.cli", "diff", "HEAD~1", "--project", project / "swcraft.yaml"],
            cwd=project,
        )

        assert completed.returncode == 1, completed.stdout + completed.stderr
        assert "retired layout in revision 'HEAD~1': 12 element files in 'data_types'" in completed.stderr
        assert str(project) not in completed.stderr
        assert "tmp" not in completed.stderr.replace(str(project), "")

    def test_a_revision_on_the_current_layout_is_read(self, project: Path) -> None:
        _commit_all(project, "the current layout")
        assert load_model_state(_model(project), rev="HEAD")["implementation_types"]


class TestTheEditingProtocol:
    """Answered as a model root the protocol cannot read: the code, no address, the kernel's sentence."""

    @pytest.mark.parametrize(
        "request_",
        [
            {"operation": "discover"},
            {"operation": "read", "address": {"layer": "interfaces"}},
            {
                "operation": "change_set",
                # A well-formed request: the preflight answers before the baseline is compared.
                "baseline": "rev1:DPkmAK4jdtM_OVToPlkB3A",
                "intent": "Record a synthetic requirement.",
                "operations": [
                    {
                        "operation": "create",
                        "address": {"layer": "requirements", "element": "REQ-SYNTHETIC-1"},
                        "value": {"id": "REQ-SYNTHETIC-1", "title": "Synthetic", "description": "Synthetic."},
                    }
                ],
            },
        ],
        ids=["discover", "read", "change_set"],
    )
    def test_every_request_shape_is_refused(self, project: Path, request_: dict) -> None:
        _retire_implementation_types(_model(project))
        response = handle_request(request_, project=project / "swcraft.yaml")
        assert response["response"] == "error", response
        assert response["error"] == "consistency-violation"
        # A violation about the model as a whole: no element address, the kernel's own sentence.
        assert "address" not in response
        assert response["message"].startswith("The model keeps its elements in a retired layout: 12 element files")
        assert str(project) not in str(response)

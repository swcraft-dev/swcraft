"""The experiment frame's kind-independent pieces (``experiment_frame.py``), and their parity with allocation's.

``allocation.py`` keeps its own copies, because moving them would shift the
``allocation.py`` lines the coverage oracle pins; the parity tests run both copies on the
same inputs, so the two cannot drift unseen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from carma_models import three_cores
from pydantic import BaseModel, ConfigDict, ValidationError
from swcraft_tools import allocation, experiment_frame
from swcraft_tools.experiment_frame import (
    ConfigDocumentError,
    layer_link_targets,
    model_digest,
    read_config_document,
    refusal_lines,
    report_name_refusal,
    write_report_files,
)

# The configuration document


class TestReadConfigDocument:
    def test_it_parses_and_digests_the_bytes(self, tmp_path: Path) -> None:
        import hashlib

        path = tmp_path / "x.yaml"
        path.write_text("name: x\nsearch:\n  candidates: 3\n", encoding="utf-8", newline="\n")
        document, digest = read_config_document(path)
        assert document == {"name": "x", "search": {"candidates": 3}}
        assert digest == f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"

    @pytest.mark.parametrize("text", ["", "# only a comment\n"])
    def test_an_empty_file_is_none(self, tmp_path: Path, text: str) -> None:
        path = tmp_path / "x.yaml"
        path.write_text(text, encoding="utf-8", newline="\n")
        assert read_config_document(path)[0] is None

    def test_a_crlf_copy_digests_differently_and_parses_equal(self, tmp_path: Path) -> None:
        lf, crlf = tmp_path / "lf.yaml", tmp_path / "crlf.yaml"
        lf.write_bytes(b"name: x\n")
        crlf.write_bytes(b"name: x\r\n")
        (a, da), (b, db) = read_config_document(lf), read_config_document(crlf)
        assert a == b and da != db

    def test_not_utf8_raises_unicode_decode_error(self, tmp_path: Path) -> None:
        path = tmp_path / "x.yaml"
        path.write_bytes(b"name: \xff\xfe\n")
        with pytest.raises(UnicodeDecodeError):
            read_config_document(path)

    def test_invalid_yaml_names_the_problem_and_no_path(self, tmp_path: Path) -> None:
        path = tmp_path / "secret-name.yaml"
        path.write_text("name: [unclosed\n", encoding="utf-8", newline="\n")
        with pytest.raises(ConfigDocumentError) as caught:
            read_config_document(path)
        assert str(caught.value).startswith("the configuration file is not valid YAML: ")
        assert "secret-name" not in str(caught.value) and str(tmp_path) not in str(caught.value)

    def test_a_missing_file_raises_os_error(self, tmp_path: Path) -> None:
        with pytest.raises(OSError):
            read_config_document(tmp_path / "absent.yaml")


# The refusal lines and the report name


class _Inner(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    count: int = 1


class _Outer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    inner: _Inner = _Inner()


def _validation_error(document: dict[str, Any]) -> ValidationError:
    try:
        _Outer.model_validate(document)
    except ValidationError as exc:
        return exc
    raise AssertionError("the document validated")


class TestRefusalLines:
    def test_each_line_names_its_dotted_key(self) -> None:
        error = _validation_error({"inner": {"count": "3", "extra": 1}, "top": 2})
        lines = refusal_lines(error)
        assert "'inner.count': Input should be a valid integer" in lines
        assert "'inner.extra': Extra inputs are not permitted" in lines
        assert "'top': Extra inputs are not permitted" in lines

    def test_parity_with_allocation(self) -> None:
        error = _validation_error({"inner": {"count": True}, "kind": "x"})
        assert refusal_lines(error) == allocation._refusal_lines(error)


#: Every spelling allocation's own tests refuse as a report name, and a few that pass.
_NAMES = ["CON", "con", "NUL.txt", "a/b", "a\\b", "relieve.", "relieve ", "", ".", "..", "relieve", "trace", "x-1"]


class TestReportName:
    @pytest.mark.parametrize(
        ("value", "phrase"),
        [
            ("CON", "names a device Windows reserves (CON)"),
            ("a/b", "name"),
            ("relieve.", "ends in a space or a dot"),
            ("relieve ", "ends in a space or a dot"),
        ],
    )
    def test_a_refused_name(self, value: str, phrase: str) -> None:
        refusal = report_name_refusal(value)
        assert refusal is not None and phrase in refusal

    @pytest.mark.parametrize("value", ["trace", "relieve", "x-1"])
    def test_an_accepted_name(self, value: str) -> None:
        assert report_name_refusal(value) is None

    @pytest.mark.parametrize("value", _NAMES)
    def test_parity_with_allocation(self, value: str) -> None:
        """The same refusal as :class:`~swcraft_tools.allocation.AllocationConfig`'s name validator, word for word."""
        try:
            allocation.AllocationConfig.model_validate({"name": value})
        except ValidationError as exc:
            expected: str | None = allocation._refusal_lines(exc)[0].removeprefix("'name': ")
        else:
            expected = None
        assert report_name_refusal(value) == expected


# The model digest and the link targets


class TestModelDigest:
    def test_parity_with_allocation(self, tmp_path: Path) -> None:
        root = three_cores().write(tmp_path / "model")
        assert model_digest(root) == allocation.model_digest(root)

    def test_a_content_change_changes_it_and_a_stray_file_does_not(self, tmp_path: Path) -> None:
        root = three_cores().write(tmp_path / "model")
        before = model_digest(root)
        (root / "notes.txt").write_text("not a layer file\n", encoding="utf-8", newline="\n")
        assert model_digest(root) == before
        task = next((root / "tasks").glob("*.yaml"))
        task.write_text(task.read_text(encoding="utf-8") + "# edit\n", encoding="utf-8", newline="\n")
        assert model_digest(root)[0] != before[0]


class TestLayerLinkTargets:
    def test_parity_with_allocation(self, tmp_path: Path) -> None:
        root = three_cores().write(tmp_path / "model")
        directory = tmp_path / "dist" / "experiments" / "x"
        for layer in ("swc", "tasks", "hardware"):
            assert layer_link_targets(root, layer, directory) == allocation._layer_targets(root, layer, directory)

    def test_a_target_is_posix_and_relative(self, tmp_path: Path) -> None:
        root = three_cores().write(tmp_path / "model")
        targets = layer_link_targets(root, "hardware", tmp_path / "dist" / "experiments" / "x")
        assert targets and all(target.startswith("../../../model/hardware/") for target in targets.values())


# The paired write


def _three(directory: Path, tag: str) -> list[tuple[Path, str]]:
    return [(directory / name, f"{tag} {name}\n") for name in ("result.json", "proposals.json", "report.md")]


class TestWriteReportFiles:
    def test_it_writes_every_file_and_leaves_no_partial(self, tmp_path: Path) -> None:
        directory = tmp_path / "out" / "x"
        write_report_files(directory, _three(directory, "new"))
        assert {path.name: path.read_text(encoding="utf-8") for path in directory.iterdir()} == {
            "result.json": "new result.json\n",
            "proposals.json": "new proposals.json\n",
            "report.md": "new report.md\n",
        }

    def test_the_renames_run_in_the_given_order(self, tmp_path: Path, monkeypatch) -> None:
        renamed: list[str] = []
        real_replace = experiment_frame.os.replace

        def recording(source: Any, target: Any) -> None:
            renamed.append(Path(target).name)
            real_replace(source, target)

        monkeypatch.setattr(experiment_frame.os, "replace", recording)
        write_report_files(tmp_path, _three(tmp_path, "new"))
        assert renamed == ["result.json", "proposals.json", "report.md"]

    def test_after_replace_reports_each_completed_rename_and_none_that_failed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The count a caller reads when a rename between two files fails (the test tables' mixed bundle)."""
        real_replace = experiment_frame.os.replace

        def refuse_the_second(source: Any, target: Any) -> None:
            if Path(target).name == "proposals.json":
                raise PermissionError(13, "held open")
            real_replace(source, target)

        monkeypatch.setattr(experiment_frame.os, "replace", refuse_the_second)
        completed: list[str] = []
        with pytest.raises(PermissionError):
            write_report_files(
                tmp_path, _three(tmp_path, "new"), after_replace=lambda path: completed.append(path.name)
            )
        assert completed == ["result.json"]

    @pytest.mark.parametrize("module", [experiment_frame, allocation], ids=["frame", "allocation"])
    def test_a_failed_write_replaces_nothing(self, tmp_path: Path, monkeypatch, module: Any) -> None:
        """Both copies: a write failing part-way leaves the previous files and no ``*.partial``."""
        files = _three(tmp_path, "old")
        write_report_files(tmp_path, files)
        previous = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
        real_write_text = Path.write_text

        def full_disk(self: Path, *args: Any, **kwargs: Any) -> int:
            if self.name.startswith("report.md.") and self.name.endswith(".partial"):
                raise OSError(28, "No space left on device")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", full_disk)
        writer = module.write_report_files if module is experiment_frame else module._write_pair
        with pytest.raises(OSError, match="No space left on device"):
            writer(tmp_path, _three(tmp_path, "new"))
        assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == previous

    def test_a_refusing_last_look_replaces_nothing(self, tmp_path: Path) -> None:
        write_report_files(tmp_path, _three(tmp_path, "old"))
        previous = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
        seen: list[str] = []

        def refuse() -> None:
            seen.extend(sorted(path.name.split(".")[0] for path in tmp_path.glob("*.partial")))
            raise RuntimeError("changed")

        with pytest.raises(RuntimeError, match="changed"):
            write_report_files(tmp_path, _three(tmp_path, "new"), refuse)
        assert seen == ["proposals", "report", "result"]
        assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == previous

    def test_a_temporary_name_already_taken_is_retried(self, tmp_path: Path, monkeypatch) -> None:
        draws = iter([b"\x00" * 8, b"\x00" * 8, b"\x01" * 8, b"\x02" * 8])
        monkeypatch.setattr(experiment_frame.os, "urandom", lambda size: next(draws))
        taken = tmp_path / f"result.json.{'00' * 8}.partial"
        taken.write_text("held\n", encoding="utf-8", newline="\n")
        write_report_files(tmp_path, [(tmp_path / "result.json", "{}\n"), (tmp_path / "report.md", "# r\n")])
        assert taken.read_text(encoding="utf-8") == "held\n"
        assert sorted(path.name for path in tmp_path.iterdir()) == sorted(["report.md", "result.json", taken.name])

    def test_a_stale_partial_is_left_alone(self, tmp_path: Path) -> None:
        stale = tmp_path / "report.md.partial"
        stale.write_text("stale\n", encoding="utf-8", newline="\n")
        write_report_files(tmp_path, _three(tmp_path, "new"))
        assert stale.read_text(encoding="utf-8") == "stale\n"

"""Tests for a query where over a derived value: a rating, a boolean and an integer leaf, in the kernel and the CLI."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import AGENT_TASK_BENCH
from swcraft_tools import query
from swcraft_tools.model_state import load_model_state

# ---------------------------------------------------------------------------
# A where over a derived value: the kernel applies it, the CLI accepts it
# ---------------------------------------------------------------------------

#: Question 14 of the agent task benchmark as rows: the components rated ``B``, decompositions included.
_RATED_B = {
    "address": {"layer": "swc"},
    "form": "index",
    "derived": ["effective_level"],
    "where": [{"field": "derived.effective_level.rating", "equals": "B"}],
}


class TestAWhereOverADerivedValueInTheKernelAndTheCli:
    """``where`` names ``derived.NAME.PATH``: applied once the values exist, refused where the row lacks it."""

    @staticmethod
    def _answer(request: dict[str, Any]) -> query.Slice:
        from swcraft_tools.validator import DERIVED, derive_rows

        state = load_model_state(AGENT_TASK_BENCH / "model")
        return query.answer_request(state, query.parse_request(request), vocabulary=DERIVED, derive=derive_rows)

    def test_the_kernel_keeps_the_rows_the_clause_holds_for(self) -> None:
        answered = self._answer(_RATED_B)
        assert [row["element"] for row in answered.identities] == [
            "BrakeController",
            "BrakeTemperatureSensor",
            "BrakeThermalProtection",
        ]
        assert [clause.as_json() for clause in answered.where] == _RATED_B["where"]

    def test_a_declared_and_a_derived_clause_compose(self) -> None:
        request = {**_RATED_B, "where": [*_RATED_B["where"], {"field": "name", "equals": "BrakeController"}]}
        assert [row["element"] for row in self._answer(request).identities] == ["BrakeController"]

    def test_the_page_counts_the_filtered_rows(self) -> None:
        answered = self._answer({**_RATED_B, "limit": 2})
        assert answered.page is not None and answered.page.total == 3 and answered.page.returned == 2

    @pytest.mark.parametrize(
        ("request_body", "message"),
        [
            pytest.param(
                {"address": {"layer": "swc"}, "where": _RATED_B["where"]},
                "a where over a derived value asks for it in `derived`, and this request's `derived` is absent",
                id="no-derived",
            ),
            pytest.param(
                {**_RATED_B, "derived": ["partitions"]},
                "a where over a derived value asks for it in `derived`, and this request's `derived` names "
                "['partitions']",
                id="another-derived",
            ),
            pytest.param(
                {**_RATED_B, "where": [{"field": "derived.effective_level.grade", "equals": "B"}]},
                "names no value of the row: a where over a derived value names one value of a computed value the "
                "request asks for, and this row's are ['derived.effective_level.level', "
                "'derived.effective_level.rating']",
                id="a-path-the-shape-lacks",
            ),
            pytest.param(
                {**_RATED_B, "where": [{"field": "derived.effective_level", "equals": "B"}]},
                "names no value of the row",
                id="an-object-not-a-value",
            ),
        ],
    )
    def test_the_two_refusals(self, request_body: dict[str, Any], message: str) -> None:
        with pytest.raises(query.QueryError) as refused:
            self._answer(request_body)
        assert message in str(refused.value)

    def test_a_joined_reads_where_over_its_own_derived_value(self) -> None:
        request = {
            "address": {"layer": "tasks"},
            "form": "index",
            "join": [
                {
                    "with": {
                        "address": {"layer": "tasks"},
                        "derived": ["core"],
                        "where": [{"field": "derived.core.core", "equals": "Core1"}],
                    },
                    "on": [{"left": "element", "right": "element"}],
                    "as": "task",
                }
            ],
        }
        assert [row["element"] for row in self._answer(request).identities] == ["Task_50ms"]
        unasked = {"address": {"layer": "tasks"}, "where": [{"field": "derived.core.core", "equals": "Core1"}]}
        refused = {**request, "join": [{**request["join"][0], "with": unasked}]}
        with pytest.raises(query.QueryError, match="^join 'task': `where` 'derived.core.core': a where over"):
            self._answer(refused)

    @pytest.mark.parametrize(
        ("derived", "clause"),
        [
            pytest.param("partitions", {"field": "derived.partitions.partition", "exists": False}, id="exists"),
            pytest.param("partitions", {"field": "derived.partitions.core", "equals": None}, id="equals-null"),
            pytest.param(
                "connection_ends", {"field": "derived.connection_ends.provider.swc", "exists": False}, id="ends"
            ),
        ],
    )
    def test_absence_over_a_set_valued_value_is_refused(self, derived: str, clause: dict[str, Any]) -> None:
        layer = "connectors" if derived == "connection_ends" else "swc"
        body = {"address": {"layer": layer}, "form": "index", "derived": [derived], "where": [clause]}
        with pytest.raises(query.QueryError, match="a computed value with no value yields no row"):
            self._answer(body)
        joined = {
            "address": {"layer": layer},
            "form": "index",
            "join": [
                {
                    "with": {key: body[key] for key in ("address", "derived", "where")},
                    "on": [{"left": "element", "right": "element"}],
                    "as": "it",
                }
            ],
        }
        with pytest.raises(query.QueryError, match="^join 'it': `where` .* set-valued computed value"):
            self._answer(joined)

    def test_absence_over_a_single_valued_value_is_not_refused(self) -> None:
        for clause in (
            {"field": "derived.core.core", "exists": False},
            {"field": "derived.core.core", "equals": None},
            {"field": "derived.effective_level.level", "exists": False},
        ):
            derived = clause["field"].split(".")[1]
            layer = "tasks" if derived == "core" else "swc"
            body = {"address": {"layer": layer}, "form": "index", "derived": [derived], "where": [clause]}
            assert list(self._answer(body).identities) == []

    def test_the_kernel_refusal_reads_the_vocabularys_rows(self) -> None:
        from swcraft_tools.validator import DERIVED

        shape = {"element": None, "derived": {"core": {"partition": None, "core": None}}}
        absent = [query.Clause(field="derived.core.core", exists=False)]
        assert query.derived_where_refusal(absent, ["core"], shape, DERIVED) is None
        set_shape = {"element": None, "derived": {"partitions": {"partition": None, "core": None}}}
        carried = [query.Clause(field="derived.partitions.core", exists=True)]
        assert query.derived_where_refusal(carried, ["partitions"], set_shape, DERIVED) is None
        other = [query.Clause(field="derived.partitions.core", equals="Core0")]
        assert query.derived_where_refusal(other, ["partitions"], set_shape, DERIVED) is None
        absent_set = [query.Clause(field="derived.partitions.core", exists=False)]
        refused = query.derived_where_refusal(absent_set, ["partitions"], set_shape, DERIVED)
        assert refused is not None and refused[0] == "derived.partitions.core"
        assert query.derived_where_refusal(absent_set, ["partitions"], set_shape) is None, "no vocabulary, no rows"

    def test_the_set_valued_rows_value_is_the_validators(self) -> None:
        # `query` may not import `validator`, so it spells the value; this ties the two spellings.
        from swcraft_tools.validator.derived import _ONE_PER_VALUE

        assert query.SET_VALUED_ROWS == _ONE_PER_VALUE

    def test_rows_matching_reads_exists_as_the_filter_does(self) -> None:
        rows = [
            {"element": "A", "derived": {"core": {"partition": "P", "core": "C"}}},
            {"element": "B", "derived": {"core": {"partition": "P", "core": None}}},
            {"element": "C", "derived": {"core": {"partition": "P", "core": ""}}},
        ]
        carried = query.rows_matching(rows, [query.Clause(field="derived.core.core", exists=True)])
        assert [row["element"] for row in carried] == ["A"]
        absent = query.rows_matching(rows, [query.Clause(field="derived.core.core", exists=False)])
        assert [row["element"] for row in absent] == ["B", "C"]
        nulls = query.rows_matching(rows, [query.Clause(field="derived.core.core", equals=None)])
        assert [row["element"] for row in nulls] == ["B"]

    def test_swcraft_query_request_accepts_the_same_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        from swcraft_tools.cli import main

        request = tmp_path / "rated-b.json"
        request.write_text(json.dumps(_RATED_B), encoding="utf-8", newline="\n")
        argv = ["swcraft", "query", "--request", str(request), "--project", str(AGENT_TASK_BENCH / "swcraft.yaml")]
        monkeypatch.setattr("sys.argv", [*argv, "--format", "json"])
        capsys.readouterr()
        main()
        rows = json.loads(capsys.readouterr().out)["rows"]
        assert [row["element"] for row in rows] == [
            "BrakeController",
            "BrakeTemperatureSensor",
            "BrakeThermalProtection",
        ]
        refused = tmp_path / "refused.json"
        refused.write_text(json.dumps({**_RATED_B, "derived": ["partitions"]}), encoding="utf-8", newline="\n")
        monkeypatch.setattr("sys.argv", ["swcraft", "query", "--request", str(refused), "--project", argv[-1]])
        with pytest.raises(SystemExit) as exited:
            main()
        assert exited.value.code == 1
        assert "a where over a derived value asks for it in `derived`" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# A where over the requirements layer's values: a boolean and an integer leaf
# ---------------------------------------------------------------------------

#: Question 19 of the agent task benchmark: the requirements nothing realises.
_UNALLOCATED = {
    "address": {"layer": "requirements"},
    "form": "index",
    "derived": ["allocation"],
    "where": [{"field": "derived.allocation.allocated", "equals": False}],
}


class TestAWhereOverABooleanAndAnIntegerLeaf:
    """The widened leaves need no new scalar handling: ``values_equal`` tells ``false`` from ``0``."""

    _answer = staticmethod(TestAWhereOverADerivedValueInTheKernelAndTheCli._answer)

    def test_equals_false_on_a_boolean_leaf(self) -> None:
        answered = self._answer(_UNALLOCATED)
        assert len(answered.identities) == 31
        assert all(
            row["derived"]["allocation"] == {"sites": 0, "elements": 0, "allocated": False}
            for row in answered.identities
        )

    def test_false_is_not_zero_and_zero_is_not_false(self) -> None:
        zero = {**_UNALLOCATED, "where": [{"field": "derived.allocation.allocated", "equals": 0}]}
        assert list(self._answer(zero).identities) == []
        sites = {**_UNALLOCATED, "where": [{"field": "derived.allocation.sites", "equals": 0}]}
        assert len(self._answer(sites).identities) == 32, "the 31 and the stub, which has no site either"
        not_false = {**_UNALLOCATED, "where": [{"field": "derived.allocation.sites", "equals": False}]}
        assert list(self._answer(not_false).identities) == []

    def test_equals_on_an_integer_leaf(self) -> None:
        request = {
            "address": {"layer": "requirements"},
            "form": "index",
            "derived": ["realised_by"],
            "where": [
                {"field": "derived.realised_by.stated", "equals": 1},
                {"field": "id", "equals": "REQ_BTP_DIAGNOSTICS"},
            ],
        }
        assert len(self._answer(request).identities) == 4

    def test_a_stub_is_null_and_exists_false_selects_it(self) -> None:
        request = {**_UNALLOCATED, "where": [{"field": "derived.allocation.allocated", "exists": False}]}
        assert [row["element"] for row in self._answer(request).identities] == ["HAZ_BTP_BRAKE_FADE"]

    def test_exists_on_a_null_integer_leaf(self, tmp_path: Path) -> None:
        """A bare link states no version: ``exists: true`` on ``stated`` drops it, the stated ones stay.

        ``exists: false`` is refused there as at any leaf of a set-valued value (``stale_links``
        yields no row where it has no value), so a ``null`` leaf is read through ``exists: true``.
        """
        project = tmp_path / "bench"
        shutil.copytree(AGENT_TASK_BENCH, project)
        interface = project / "model" / "interfaces" / "IBrakeTemperature.yaml"
        text = interface.read_text(encoding="utf-8")
        assert "REQ_BTP_TEMPERATURE_RANGE@1" in text
        interface.write_text(
            text.replace("REQ_BTP_TEMPERATURE_RANGE@1", "REQ_BTP_TEMPERATURE_RANGE"), encoding="utf-8", newline="\n"
        )
        from swcraft_tools.validator import DERIVED, derive_rows

        state = load_model_state(project / "model")

        def stale(where: list[dict[str, Any]]) -> list[tuple[str, Any]]:
            request: dict[str, Any] = {
                "address": {"layer": "requirements"},
                "form": "index",
                "derived": ["stale_links"],
            }
            if where:
                request["where"] = where
            answered = query.answer_request(state, query.parse_request(request), vocabulary=DERIVED, derive=derive_rows)
            return [
                (row["derived"]["stale_links"]["element"], row["derived"]["stale_links"]["stated"])
                for row in answered.identities
            ]

        assert stale([]) == [("IBrakeDiagnostics", 1), ("IBrakeTemperature", None), ("IThermalProtectionStatus", 1)]
        assert stale([{"field": "derived.stale_links.stated", "exists": True}]) == [
            ("IBrakeDiagnostics", 1),
            ("IThermalProtectionStatus", 1),
        ]
        with pytest.raises(query.QueryError, match="a set-valued computed value"):
            stale([{"field": "derived.stale_links.stated", "exists": False}])

    def test_count_over_a_boolean_leaf_through_the_engine(self) -> None:
        from swcraft_tools.editing.engine import handle_request

        project = AGENT_TASK_BENCH / "swcraft.yaml"
        answered = handle_request({"operation": "read", **_UNALLOCATED, "form": "count"}, project=project)
        assert (answered["response"], answered["count"]) == ("count", 31)

    def test_swcraft_query_request_accepts_the_same_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        from swcraft_tools.cli import main

        request = tmp_path / "unallocated.json"
        request.write_text(json.dumps(_UNALLOCATED), encoding="utf-8", newline="\n")
        argv = ["swcraft", "query", "--request", str(request), "--project", str(AGENT_TASK_BENCH / "swcraft.yaml")]
        monkeypatch.setattr("sys.argv", [*argv, "--format", "json"])
        capsys.readouterr()
        main()
        rows = json.loads(capsys.readouterr().out)["rows"]
        assert len(rows) == 31
        assert rows[0] == {
            "element": "SWR-BRK-005",
            "derived": {"allocation": {"sites": 0, "elements": 0, "allocated": False}},
        }

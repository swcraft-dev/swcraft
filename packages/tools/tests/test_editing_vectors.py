"""The contract vectors, executed: each one's baseline seeded, its request dispatched through the engine.

``test_editing_protocol_contracts.py`` validates the vectors statically; this module runs them and
holds each answer to the vector's expectation. Everything the vectors cannot state themselves (the
seeded baselines, the precondition checks, the comparison rule for the two values an implementation
may produce differently) lives in ``editing_vector_fixtures``, which imports the engine lazily so a
missing package fails per vector rather than failing this file's collection.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
from conftest import model_element_path
from fixture_scope_support import classes_consuming
from swcraft_tools.validator import check_cross_layer_consistency

#: Vector name -> the task that closes it. A listed vector runs as a strict ``xfail``, so one that
#: starts passing while listed fails the suite.
OPEN_VECTORS: dict[str, str] = {
    # Empty: every shipped vector runs unmarked.
}


def _vector_parameters() -> list[Any]:
    """Every vector as a parametrize entry, ``xfail``-marked while it is open."""
    parameters = []
    for name in sorted(fixtures.VECTORS):
        task = OPEN_VECTORS.get(name)
        marks = [pytest.mark.xfail(strict=True, reason=f"open vector: {task}")] if task else []
        parameters.append(pytest.param(name, marks=marks, id=name))
    return parameters


class TestVectors:
    """The acceptance suite proper: one case per shipped vector."""

    @pytest.mark.parametrize("vector_name", _vector_parameters())
    def test_vector(self, vector_name: str, tmp_path: Path) -> None:
        vector = fixtures.VECTORS[vector_name]
        project = fixtures.build_fixture(tmp_path / "project", vector_name)
        fixtures.check_preconditions(project, vector)

        token = fixtures.baseline_token(project, vector_name)
        request = fixtures.prepare_request(vector, token)

        if vector["context"].get("baseline_state") == "stale":
            # After the token has been taken and substituted, never before: the
            # vector must fail for its own stated reason and not incidentally.
            fixtures.make_stale(project)

        response = fixtures.dispatch(
            request,
            project=project,
            approved_scope=fixtures.approved_scope(vector),
            review=fixtures.host_review(vector),
            # `None` for every vector but the one recorded on the MCP server's
            # channel (`fixtures.HOST_CHANNEL_VECTORS`): the others record `summary`.
            discover_default=fixtures.discover_default_for(vector_name),
        )

        if vector["kind"] == "example":
            # Recorded as the response leaves the server: a whole element's removal
            # carries its `before` in summary form, the projection
            # `swcraft_mcp.server._handle` applies at the edge. The engine's own
            # whole delta is `test_editing_delta.py`'s.
            fixtures.assert_response_matches(
                fixtures.as_sent(response), vector["expected"]["response"], request=request
            )
        else:
            fixtures.assert_error_matches(response, vector["expected"])


def _committed_change_set_vectors() -> list[str]:
    """Every example whose expected response is a ``committed`` change set."""
    return sorted(
        name
        for name, vector in fixtures.VECTORS.items()
        if isinstance(vector.get("expected", {}).get("response"), dict)
        and vector["expected"]["response"].get("response") == "change_set"
        and vector["expected"]["response"].get("status") == "committed"
    )


class TestTheCommittedVectorsAskTheSafetyQuestionTheirNotesDescribe:
    """What replay alone cannot see: whether the host was asked the second (safety) question.

    ``host_review`` answers from the vector's own ``status`` and the expected response carries no
    verdict, so :class:`TestVectors` replays a committed vector green whether or not the engine
    classified a weakening; here each proposal's safety changes are held to the kinds named.
    """

    #: Measured from the live engine, one entry per committed change-set vector.
    SAFETY_KINDS: dict[str, list[str]] = {
        "create-at-occupied-storage-target": [],
        # The partition's level and argument (contract 9.0.0): the raised level is
        # the non-weakening direction and is committed on the delta answer alone.
        "create-partition-carrying-coexistence": ["declare_partition_coexistence"],
        # The partition's trust class (contract 9.1.0): a movement between the two
        # confined classes asks nothing.
        "create-partition-trusted": ["grant_partition_trust"],
        "create-port-measure": ["declare_measure"],
        "remove-memory-resource-with-referrers-cleared": ["delete_resource"],
        "update-at-relocated-storage-target": [],
        "update-core-asil-capability": ["raise_capability"],
        "update-memory-accessible-and-threshold": ["raise_threshold", "widen_access"],
        "update-partition-asil-lowered": ["lower_allocation"],
        "update-partition-asil-raised": [],
        "update-partition-coexistence": ["declare_partition_coexistence"],
        "update-partition-trust-confined": [],
        "update-partition-trust-granted": ["grant_partition_trust"],
        "update-partition-trust-protected": ["declare_partition_protection"],
        "update-task-coexistence": ["declare_coexistence"],
        "update-task-into-argued-partition": ["declare_partition_coexistence"],
    }

    def test_every_committed_vector_is_named_here(self) -> None:
        assert sorted(self.SAFETY_KINDS) == _committed_change_set_vectors()

    @pytest.mark.parametrize("vector_name", sorted(SAFETY_KINDS))
    def test_the_proposal_carries_exactly_the_safety_changes_named(self, vector_name: str, tmp_path: Path) -> None:
        vector = fixtures.VECTORS[vector_name]
        project = fixtures.build_fixture(tmp_path / "project", vector_name)
        request = fixtures.prepare_request(vector, fixtures.current_baseline_token(project))
        answer = fixtures.host_review(vector)
        asked: list[list[str]] = []

        def review(proposal: Any) -> Any:
            asked.append([change.kind.value for change in proposal.safety.changes])
            return answer(proposal)

        response = fixtures.dispatch(
            request, project=project, approved_scope=fixtures.approved_scope(vector), review=review
        )

        assert response.get("status") == "committed", response
        assert asked == [self.SAFETY_KINDS[vector_name]]


class TestTheOverBudgetChainTheVectorDescribes:
    """The chain `read-layer-full-over-budget.json` states in prose only, replayed on its fixture.

    ``assert_error_matches`` never reads ``hints`` (``vector.schema.json`` has no key for a chain),
    so the vector's "summary, then index, and no paged third" is asserted here on the
    three-component layer it is about; ``test_editing_budget.py``'s twenty-component fixture is
    offered the paged step and cannot witness its absence.

    What it does not reach: every other negative's chain; ``hints`` on no other code is
    ``assert_error_matches``'s check.
    """

    VECTOR = "read-layer-full-over-budget"

    @staticmethod
    def _budget_constants() -> tuple[int, int]:
        """``(budget, hint page size)``, imported inside the call (see ``TestTheEngineSeamIsLazy``)."""
        from swcraft_tools.editing import read

        return read.RESPONSE_BUDGET_WIRE_BYTES, read.HINT_PAGE_SIZE

    def _refusal(self, tmp_path: Path) -> dict[str, object]:
        vector = fixtures.VECTORS[self.VECTOR]
        project = fixtures.build_fixture(tmp_path / "project", self.VECTOR)
        fixtures.check_preconditions(project, vector)
        request = fixtures.prepare_request(vector, fixtures.current_baseline_token(project))
        return fixtures.dispatch(
            request,
            project=project,
            approved_scope=fixtures.approved_scope(vector),
            review=fixtures.host_review(vector),
        )

    def test_the_chain_is_the_two_narrower_forms_in_the_narrowing_order(self, tmp_path: Path) -> None:
        response = self._refusal(tmp_path)
        assert response["hints"] == [
            {"operation": "read", "address": {"layer": "swc"}, "form": "summary"},
            {"operation": "read", "address": {"layer": "swc"}, "form": "index"},
        ], "the vector's `notes` state this chain, in this order, and nothing else replayed it"

    def test_no_hint_carries_a_page_because_the_layer_is_one_page(self, tmp_path: Path) -> None:
        """Three components fit one hint page, so a paged step would replay the refused request."""
        response = self._refusal(tmp_path)
        assert all("limit" not in hint and "after" not in hint for hint in response["hints"])
        assert len(response["hints"]) == 2, "the two narrower forms and no third step"
        _budget, hint_page_size = self._budget_constants()
        assert response["measured"]["elements"] <= hint_page_size, (
            "the reason there is no paged step, read off the constant rather than restated"
        )

    def test_the_measured_block_names_the_size_the_budget_and_the_count(self, tmp_path: Path) -> None:
        """`measured` is asserted present by the shared comparison and never read."""
        measured = self._refusal(tmp_path)["measured"]
        assert set(measured) == {"wire_bytes", "budget", "elements"}
        assert measured["budget"] == self._budget_constants()[0]
        assert measured["wire_bytes"] > measured["budget"], "the refusal is about a response that is too big"
        assert measured["elements"] == 3, "the three-component layer the vector's prose is about"


class TestTheDiscoverRefusalChainTheVectorDescribes:
    """`negative/discover-project-full-over-budget.json`'s chain, replayed on its fixture and the real budget.

    The vector's `notes` state the chain (the scope at `summary`, then one `full` per declared
    layer) and `assert_error_matches` reads `hints` on no negative.

    What it does not reach: the sizes. `test_discover_budget.py` pins the project-wide figure above
    the budget and every per-layer `full` under it.
    """

    VECTOR = "discover-project-full-over-budget"

    def _refusal(self, tmp_path: Path) -> tuple[Path, dict[str, object]]:
        vector = fixtures.VECTORS[self.VECTOR]
        project = fixtures.build_fixture(tmp_path / "project", self.VECTOR)
        fixtures.check_preconditions(project, vector)
        request = fixtures.prepare_request(vector, fixtures.current_baseline_token(project))
        return project, fixtures.dispatch(
            request,
            project=project,
            approved_scope=fixtures.approved_scope(vector),
            review=fixtures.host_review(vector),
        )

    def test_the_chain_is_the_summary_then_one_full_per_layer(self, tmp_path: Path) -> None:
        from swcraft_tools.editing import registry as reg

        _project, response = self._refusal(tmp_path)
        assert response["hints"] == [{"operation": "discover", "form": "summary"}] + [
            {"operation": "discover", "layer": name, "form": "full"} for name in reg.current().layer_names
        ]
        assert "addresses" not in response, "a discovery is not addressed"
        assert response["measured"]["elements"] == len(reg.current().layer_names)

    def test_every_hint_answers_and_the_fifteen_full_ones_are_the_shipped_vectors(self, tmp_path: Path) -> None:
        """Every hint answers, and each per-layer `full` answer is the one its example vector records."""
        project, response = self._refusal(tmp_path)
        for hint in response["hints"]:
            answer = fixtures.dispatch(dict(hint), project=project, approved_scope=None, review=None)
            assert answer["response"] == "discover", hint
            if hint.get("form") == "full":
                name = f"discover-layer-{hint['layer'].replace('_', '-')}-full"
                recorded = fixtures.VECTORS[name]["expected"]["response"]
                assert answer["layers"] == recorded["layers"], name


@pytest.mark.xdist_group("editing-vectors-baseline")
class TestTheBaselineTokenIsAFunctionOfTheModelState:
    """The token relation, checked once over the whole vector set.

    The registry is one class-scoped fixture: accumulated per case, the relation would hold
    vacuously under ``-k``, per worker under ``xdist``, and name the wrong pair on a collision.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def registry(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[str, str]]:
        root = tmp_path_factory.mktemp("token-registry")
        built: dict[str, tuple[str, str]] = {}
        for name in sorted(fixtures.VECTORS):
            project = fixtures.build_fixture(root / name, name)
            built[name] = (
                fixtures.model_state_key(project / "model"),
                fixtures.baseline_token(project, name),
            )
        return built

    def test_the_registry_covers_every_vector(self, registry: dict[str, tuple[str, str]]) -> None:
        assert set(registry) == set(fixtures.VECTORS)

    def test_only_a_retired_layout_has_no_token(self, registry: dict[str, tuple[str, str]]) -> None:
        # Stated both ways: a readable fixture whose token went missing would
        # otherwise slip out of the two relations below unnoticed.
        tokenless = {name for name, (_state, token) in registry.items() if token is None}
        assert tokenless == {name for name, seed in fixtures.SEEDS.items() if seed.retired_layout}
        assert tokenless, "no vector describes a retired layout; the case above would quantify over nothing"

    def test_distinct_model_states_never_share_a_token(self, registry: dict[str, tuple[str, str]]) -> None:
        fixtures.assert_tokens_distinct(registry)

    def test_one_model_state_always_produces_one_token(self, registry: dict[str, tuple[str, str]]) -> None:
        # The converse, which a randomly drawn token would fail: `staged` returns
        # the request's own token precisely because the state did not move.
        fixtures.assert_equal_states_share_a_token(registry)

    def test_neither_direction_holds_vacuously(self, registry: dict[str, tuple[str, str]]) -> None:
        # Both shapes must occur, or one of the two cases above quantifies over
        # nothing.
        states = {state for state, _token in registry.values()}
        assert len(states) > 1, "every fixture is the same model state; distinctness is vacuous"
        assert len(states) < len(registry), "no two fixtures share a state; the converse is vacuous"


class TestTheBaseModelIsFrozen:
    """``examples/basic`` is a demo project; here it is the acceptance base."""

    def test_the_base_model_digest_is_the_pinned_one(self) -> None:
        actual = fixtures.model_state_key(fixtures.BASE_MODEL_ROOT)
        assert actual == fixtures.BASE_MODEL_DIGEST, (
            f"`examples/basic/model` changed (digest {actual}). Every contract vector is seeded from it and "
            "several state its exact contents — `read-data-types-layer` expects the whole implementation_types "
            "layer to be two elements, and every `create` vector needs its target absent. Re-read the vectors that "
            "depend on the base, then update `editing_vector_fixtures.BASE_MODEL_DIGEST` in the same commit."
        )


class TestTheLedgerIsComplete:
    """The ``xfail`` table describes exactly the shipped vector set."""

    def test_every_open_vector_is_a_real_vector(self) -> None:
        unknown = sorted(set(OPEN_VECTORS) - set(fixtures.VECTORS))
        assert not unknown, f"the ledger marks vectors that do not exist: {unknown}"

    def test_the_ledger_is_closed(self) -> None:
        # Stated positively: over an empty table the per-entry task check below
        # is vacuous; it is kept for the day the table is non-empty again.
        assert OPEN_VECTORS == {}, f"vectors are still open: {sorted(OPEN_VECTORS)}"
        assert all(task.startswith("Task ") for task in OPEN_VECTORS.values())


class TestEverySeedIsDeclared:
    """A new vector cannot silently run against the bare base."""

    def test_seeds_and_vectors_are_the_same_set(self) -> None:
        assert set(fixtures.SEEDS) == set(fixtures.VECTORS), (
            f"vectors with no seed entry: {sorted(set(fixtures.VECTORS) - set(fixtures.SEEDS))}; "
            f"seed entries for no vector: {sorted(set(fixtures.SEEDS) - set(fixtures.VECTORS))}"
        )

    @pytest.mark.parametrize("vector_name", sorted(fixtures.SEEDS))
    def test_every_seed_states_why_it_exists(self, vector_name: str) -> None:
        seed = fixtures.SEEDS[vector_name]
        assert seed.reason.strip(), f"{vector_name}: the seed does not say what baseline it builds"
        for edit in seed.edits:
            assert edit.reason.strip(), f"{vector_name}: the edit at {edit.path} records no motivating rule"

    @pytest.mark.parametrize("vector_name", sorted(fixtures.SEEDS))
    def test_seed_layer_presence_agrees_with_the_vector(self, vector_name: str) -> None:
        # The seed states layer presence explicitly; where the vector also states
        # it, the two have to be the same statement.
        context = fixtures.VECTORS[vector_name]["context"]
        seed = fixtures.SEEDS[vector_name]
        if "layers_present" not in context:
            # Dropping a layer changes what pass D validates, so a silent
            # `absent_layers` would move the exchange's meaning with nothing in the
            # contract to say so.
            assert not seed.absent_layers, (
                f"{vector_name}: the seed removes {list(seed.absent_layers)} while the vector's `context` "
                "states no `layers_present`. Either the vector states the presence it is written against, "
                "or the seed leaves every layer alone."
            )
            return
        expected_absent = set(fixtures.LAYER_DIRECTORIES) - set(context["layers_present"])
        assert set(seed.absent_layers) == expected_absent


class TestSeededBaselines:
    """Each fixture is the baseline its vector states, and is valid unless declared otherwise."""

    @pytest.mark.parametrize("vector_name", sorted(fixtures.SEEDS))
    def test_preconditions_hold(self, vector_name: str, tmp_path: Path) -> None:
        project = fixtures.build_fixture(tmp_path / "project", vector_name)
        fixtures.check_preconditions(project, fixtures.VECTORS[vector_name])

    @pytest.mark.parametrize("vector_name", sorted(fixtures.SEEDS))
    def test_baseline_validity_is_the_declared_one(self, vector_name: str, tmp_path: Path) -> None:
        # Pass D validates the whole staged result and waives nothing; the
        # baseline may be invalid only where the seed says so, with its reason.
        seed = fixtures.SEEDS[vector_name]
        project = fixtures.build_fixture(tmp_path / "project", vector_name)
        if seed.retired_layout:
            # A model on a retired layout is refused, not judged: the check
            # raises the kernel's refusal rather than answering a verdict.
            from swcraft_tools.model_state import RetiredStorageError

            assert not seed.baseline_valid
            assert seed.baseline_note.strip()
            with pytest.raises(RetiredStorageError):
                check_cross_layer_consistency(str(project / "model"))
            return
        result = check_cross_layer_consistency(str(project / "model"))
        assert result.consistent == seed.baseline_valid, (
            f"{vector_name}: seed declares baseline_valid={seed.baseline_valid}, "
            f"check_cross_layer_consistency says {result.consistent} ({result.issues})"
        )
        if not seed.baseline_valid:
            assert seed.baseline_note.strip(), "an invalid baseline is an allowance, so it must state its reason"

    def test_at_least_one_baseline_is_deliberately_invalid(self) -> None:
        # Otherwise the case above asserts only the easy direction. The kinds:
        # a baseline the change set repairs
        # (`update-requirement-version-confirming-links`, a `trace.duplicate-link`
        # its one `remove` clears); a finding that is the exchange's subject and is
        # never staged (`discover-layer-hardware-absent`); an exemption, which needs
        # the finding it exempts (`create-port-measure`, `update-task-coexistence`);
        # and a retired layout, refused rather than judged (`Seed.retired_layout`).
        invalid = sorted(name for name, seed in fixtures.SEEDS.items() if not seed.baseline_valid)
        assert invalid == [
            "consistency-violation-retired-data-types-directory",
            "create-port-measure",
            "discover-layer-hardware-absent",
            "update-partition-coexistence",
            "update-requirement-version-confirming-links",
            "update-task-coexistence",
        ], invalid


class TestPreconditionCheckerBites:
    """The precondition checker is an assertion, not a comment."""

    def test_a_missing_stated_element_is_rejected(self, tmp_path: Path) -> None:
        project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
        (project / "model" / "swc" / "example" / "ExampleSWC.yaml").unlink()
        with pytest.raises(AssertionError, match="existing_elements"):
            fixtures.check_preconditions(project, fixtures.VECTORS["read-swc-element"])

    def test_a_pre_created_create_target_is_rejected(self, tmp_path: Path) -> None:
        # Seeding the target turns the exchange into an identity-conflict the
        # vector does not expect: the fixture, not the engine, is wrong.
        project = fixtures.build_fixture(tmp_path / "project", "create-data-type-alias")
        (model_element_path(project / "model", "VehicleSpeed_kph", kind="value_type")).write_text(
            "name: VehicleSpeed_kph\nkind: value_type\nbase: float32\n", encoding="utf-8"
        )
        with pytest.raises(AssertionError, match="identity-conflict"):
            fixtures.check_preconditions(project, fixtures.VECTORS["create-data-type-alias"])

    def test_a_pre_created_create_target_is_required_by_an_identity_conflict_vector(self, tmp_path: Path) -> None:
        # The other side: this vector seeds the very address its create targets.
        project = fixtures.build_fixture(tmp_path / "project", "identity-conflict-duplicate-data-type")
        assert fixtures.resolve(project / "model", {"layer": "base_types", "element": "uint16_base"}).found
        fixtures.check_preconditions(project, fixtures.VECTORS["identity-conflict-duplicate-data-type"])

    def test_a_create_at_an_occupied_target_may_not_expect_an_identity_conflict(self, tmp_path: Path) -> None:
        # A create whose plain file name another element holds takes a collision
        # suffix and commits, so a vector expecting the refusal describes an
        # exchange the store does not produce; the checker says so first.
        name = "create-at-occupied-storage-target"
        stale = deepcopy(fixtures.VECTORS[name])
        stale["kind"] = "negative"
        stale["expected"] = {
            "expected_error": "identity-conflict",
            "failure_class": "invalid",
            "violation_layer": "semantic",
            "addresses": [{"layer": "implementation_types", "element": "Speed_kph"}],
        }
        project = fixtures.build_fixture(tmp_path / "project", name)
        with pytest.raises(AssertionError, match="occupied_targets"):
            fixtures.check_preconditions(project, stale)

    def test_the_multiset_exemption_is_honoured(self, tmp_path: Path) -> None:
        # A second occurrence of a held event: a `whole_value` list is a multiset,
        # so this is never an identity-conflict.
        name = "create-and-remove-duplicate-runnable-events"
        project = fixtures.build_fixture(tmp_path / "project", name)
        created = fixtures.VECTORS[name]["input"]["operations"][2]["address"]
        assert fixtures.resolve(project / "model", created).found
        fixtures.check_preconditions(project, fixtures.VECTORS[name])

    def test_a_layer_the_vector_omits_may_not_be_present(self, tmp_path: Path) -> None:
        project = fixtures.build_fixture(tmp_path / "project", "discover-layer-hardware-absent")
        (project / "model" / "hardware").mkdir()
        with pytest.raises(AssertionError, match="layers_present"):
            fixtures.check_preconditions(project, fixtures.VECTORS["discover-layer-hardware-absent"])


class TestAddressResolution:
    """The harness's own YAML walk, since every precondition rests on it."""

    @pytest.fixture
    def model_root(self, tmp_path: Path) -> Path:
        return fixtures.build_fixture(tmp_path / "project", "read-swc-element") / "model"

    def test_a_layer_address_answers_every_element(self, model_root: Path) -> None:
        resolution = fixtures.resolve(model_root, {"layer": "hardware"})
        assert resolution.found
        # All three kinds in one identity namespace, which is what makes the
        # layer address answer them together.
        assert {element["name"] for element in resolution.value} == {"CPU", "CORE0", "PARTITION0", "RAM0"}

    def test_a_requirement_resolves_by_its_id(self, model_root: Path) -> None:
        assert fixtures.resolve(model_root, {"layer": "requirements", "element": "REQ_EXAMPLE_001"}).found

    def test_a_field_identified_member_resolves(self, model_root: Path) -> None:
        resolution = fixtures.resolve(
            model_root,
            {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}, {"member": "pOut"}]},
        )
        assert resolution.found and resolution.value["interface"] == "IExample"

    def test_a_map_key_resolves(self, model_root: Path) -> None:
        resolution = fixtures.resolve(
            model_root,
            {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "memory"}, {"key": "RAM0"}]},
        )
        assert resolution.found and resolution.value == 2048

    def test_an_absent_element_is_a_typed_miss(self, model_root: Path) -> None:
        resolution = fixtures.resolve(model_root, {"layer": "swc", "element": "NoSuchSWC"})
        assert not resolution.found and "NoSuchSWC" in resolution.missing_step

    def test_an_absent_layer_is_a_typed_miss(self, tmp_path: Path) -> None:
        model_root = fixtures.build_fixture(tmp_path / "project", "discover-layer-hardware-absent") / "model"
        resolution = fixtures.resolve(model_root, {"layer": "hardware", "element": "CORE0"})
        assert not resolution.found and "absent" in resolution.missing_step

    def test_occurrences_are_counted_for_a_multiset_member(self, tmp_path: Path) -> None:
        model_root = (
            fixtures.build_fixture(tmp_path / "project", "create-and-remove-duplicate-runnable-events") / "model"
        )
        address = {
            "layer": "swc",
            "element": "ExampleSWC",
            "path": [
                {"field": "runnables"},
                {"member": "ExampleRun"},
                {"field": "events"},
                {"member": {"type": "timing", "period": 0.01}},
            ],
        }
        resolution = fixtures.resolve(model_root, address)
        assert resolution.found and resolution.occurrences == 2


class TestComparisonContract:
    """The comparison rule bites, on both the token and the change-set id."""

    STAGED = {
        "response": "change_set",
        "status": "staged",
        "baseline": "rev1:9f4c2a7be3d18056",
        "delta": [],
    }
    COMMITTED = {
        "response": "change_set",
        "status": "committed",
        "baseline": "rev1:9f4c2a7be3d18056",
        "delta": [],
        "audit_ref": "cs-20260815T102241Z-4f19c2",
    }
    REQUEST = {"operation": "change_set", "baseline": "rev1:1111111111111111", "intent": "x", "operations": []}

    def test_a_staged_response_returning_the_requests_token_matches(self) -> None:
        actual = deepcopy(self.STAGED) | {"baseline": self.REQUEST["baseline"]}
        fixtures.assert_response_matches(actual, self.STAGED, request=self.REQUEST)

    def test_a_staged_response_returning_another_token_fails(self) -> None:
        actual = deepcopy(self.STAGED) | {"baseline": "rev1:2222222222222222"}
        with pytest.raises(AssertionError, match="byte-identical"):
            fixtures.assert_response_matches(actual, self.STAGED, request=self.REQUEST)

    def test_a_committed_response_reusing_the_requests_token_fails(self) -> None:
        actual = deepcopy(self.COMMITTED) | {"baseline": self.REQUEST["baseline"]}
        with pytest.raises(AssertionError, match="the token the commit produced"):
            fixtures.assert_response_matches(actual, self.COMMITTED, request=self.REQUEST)

    def test_a_committed_response_with_a_fresh_token_matches(self) -> None:
        actual = deepcopy(self.COMMITTED) | {"baseline": "rev1:3333333333333333"}
        fixtures.assert_response_matches(actual, self.COMMITTED, request=self.REQUEST)

    def test_a_malformed_token_fails_structurally(self) -> None:
        actual = deepcopy(self.STAGED) | {"baseline": "9f4c2a7be3d18056"}
        with pytest.raises(AssertionError, match="revisionToken"):
            fixtures.assert_response_matches(actual, self.STAGED, request=self.REQUEST)

    def test_an_audit_ref_on_a_staged_response_fails(self) -> None:
        actual = deepcopy(self.STAGED) | {
            "baseline": self.REQUEST["baseline"],
            "audit_ref": "cs-20260815T102241Z-4f19c2",
        }
        with pytest.raises(AssertionError, match="audit_ref"):
            fixtures.assert_response_matches(actual, self.STAGED, request=self.REQUEST)

    def test_a_malformed_audit_ref_fails_structurally(self) -> None:
        actual = deepcopy(self.COMMITTED) | {"baseline": "rev1:3333333333333333", "audit_ref": "cs-nope"}
        with pytest.raises(AssertionError, match="change-set id"):
            fixtures.assert_response_matches(actual, self.COMMITTED, request=self.REQUEST)

    def test_every_other_field_is_compared_by_value(self) -> None:
        actual = deepcopy(self.STAGED) | {
            "baseline": self.REQUEST["baseline"],
            "delta": [{"operation": "create", "address": {"layer": "implementation_types", "element": "x"}}],
        }
        with pytest.raises(AssertionError):
            fixtures.assert_response_matches(actual, self.STAGED, request=self.REQUEST)

    def test_two_model_states_yielding_one_token_fails(self) -> None:
        with pytest.raises(AssertionError, match="distinct model states"):
            fixtures.assert_tokens_distinct(
                {
                    "one": ("state-a", "rev1:9f4c2a7be3d18056"),
                    "two": ("state-b", "rev1:9f4c2a7be3d18056"),
                }
            )

    def test_one_model_state_shared_by_two_vectors_is_fine(self) -> None:
        # Most vectors run against the bare base, so sharing a token is the
        # normal case and must not be reported as a collision.
        fixtures.assert_tokens_distinct(
            {
                "one": ("state-a", "rev1:9f4c2a7be3d18056"),
                "two": ("state-a", "rev1:9f4c2a7be3d18056"),
            }
        )


class TestErrorComparisonContract:
    """A negative's expectation is compared on codes and addresses, never on text."""

    EXPECTED = {
        "expected_error": "unknown-field",
        "failure_class": "invalid",
        "violation_layer": "semantic",
        "addresses": [{"layer": "swc", "element": "ExampleSWC", "path": [{"field": "safety_level"}]}],
    }
    ACTUAL = {
        "response": "error",
        "error": "unknown-field",
        "failure_class": "invalid",
        "message": "swc has no field 'safety_level'",
        "addresses": [{"layer": "swc", "element": "ExampleSWC", "path": [{"field": "safety_level"}]}],
    }

    def test_a_matching_rejection_passes(self) -> None:
        fixtures.assert_error_matches(self.ACTUAL, self.EXPECTED)

    def test_a_different_code_fails(self) -> None:
        with pytest.raises(AssertionError):
            fixtures.assert_error_matches(deepcopy(self.ACTUAL) | {"error": "broken-reference"}, self.EXPECTED)

    def test_a_missing_message_fails(self) -> None:
        actual = {key: value for key, value in self.ACTUAL.items() if key != "message"}
        with pytest.raises(AssertionError, match="human-readable message"):
            fixtures.assert_error_matches(actual, self.EXPECTED)

    def test_an_unexpected_additional_block_fails(self) -> None:
        actual = deepcopy(self.ACTUAL) | {
            "additional": [{"error": "immutable-field", "failure_class": "invalid", "message": "no"}]
        }
        with pytest.raises(AssertionError, match="omitted rather than empty"):
            fixtures.assert_error_matches(actual, self.EXPECTED)

    def test_additional_violations_are_compared_in_order(self) -> None:
        expected = deepcopy(self.EXPECTED) | {
            "additional": [
                {"expected_error": "immutable-field", "failure_class": "invalid"},
                {"expected_error": "identity-conflict", "failure_class": "invalid"},
            ]
        }
        actual = deepcopy(self.ACTUAL) | {
            "additional": [
                {"error": "identity-conflict", "failure_class": "invalid", "message": "a"},
                {"error": "immutable-field", "failure_class": "invalid", "message": "b"},
            ]
        }
        with pytest.raises(AssertionError):
            fixtures.assert_error_matches(actual, expected)


class TestRequestPreparation:
    """The stand-in baseline is substituted; everything else is sent verbatim."""

    def test_the_stand_in_token_is_replaced(self) -> None:
        vector = fixtures.VECTORS["create-data-type-alias"]
        request = fixtures.prepare_request(vector, "rev1:0123456789abcdef")
        assert request["baseline"] == "rev1:0123456789abcdef"
        assert request["operations"] == vector["input"]["operations"]

    def test_a_request_without_a_baseline_is_untouched(self) -> None:
        vector = fixtures.VECTORS["read-swc-element"]
        assert fixtures.prepare_request(vector, "rev1:0123456789abcdef") == vector["input"]

    def test_an_unscoped_run_is_distinguished_from_an_empty_scope(self) -> None:
        assert fixtures.approved_scope(fixtures.VECTORS["read-swc-element"]) is None
        assert fixtures.approved_scope(fixtures.VECTORS["unauthorized-delta-empty-approved-scope"]) == []

    def test_make_stale_moves_the_model_and_leaves_it_valid(self, tmp_path: Path) -> None:
        project = fixtures.build_fixture(tmp_path / "project", "stale-baseline-change-set")
        before = fixtures.model_state_key(project / "model")
        fixtures.make_stale(project)
        assert fixtures.model_state_key(project / "model") != before
        assert check_cross_layer_consistency(str(project / "model")).consistent


class TestTheEngineSeamIsLazy:
    """``editing/`` is imported inside a call, never at module import time.

    A missing package then fails per vector instead of failing collection for the whole runner.
    """

    MODULES = (Path(__file__), Path(fixtures.__file__))

    @staticmethod
    def _imports(node: ast.AST) -> list[str]:
        names: list[str] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                names.extend(alias.name for alias in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module:
                names.append(child.module)
        return names

    @pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
    def test_no_module_level_import_of_the_engine(self, path: Path) -> None:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # `tree.body` only, never `ast.walk`: an import nested in a function is
        # exactly what this module wants, and walking would report it as an
        # offender.
        top_level = [
            name for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom) for name in self._imports(node)
        ]
        offenders = [name for name in top_level if name.startswith("swcraft_tools.editing")]
        assert not offenders, f"{path.name} imports {offenders} at module level; the engine seam must stay lazy"

    def test_the_lazy_seam_really_imports_the_engine(self) -> None:
        # Without this, deleting the import from `dispatch` would satisfy the
        # case above and the runner would never reach the engine at all.
        tree = ast.parse(Path(fixtures.__file__).read_text(encoding="utf-8"))
        deferred = [
            name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            for name in self._imports(node)
            if name.startswith("swcraft_tools.editing")
        ]
        assert deferred, "editing_vector_fixtures no longer imports the engine anywhere"


class TestThePathRefusalsSurviveTheKernelMove:
    """The four negatives whose refusal is a path question, pinned on their text.

    The placement refusal is the kernel's :func:`~swcraft_tools.model_registry.segment_refusal`;
    ``assert_error_matches`` never compares text, so a change of wording, ordering or of which rule
    answers first passes the vector suite.

    What this does not reach: the other negatives, which refuse for reasons the guards do not
    touch (schema shape, immutability, references, authorization, staleness). The two ``$defs``
    identity traversals and the storage-path envelope refusal are guarded above the registry; they
    are here because "unchanged" includes "still refused by the same layer".
    """

    #: Vector name -> its refusal text.
    MESSAGES = {
        "traversal-placement-value-create-payload": (
            "swc group '../../../../tmp/evil' must not contain path separators, '..' or NUL"
        ),
        # The `operation 0:` prefix is pass A's framing, not the guard's text:
        # every refusal of an operation names its position (without it, two
        # identically malformed addresses were indistinguishable and crashed
        # pass A's "`additional` never repeats the primary" rule). A read has no
        # operation position, so its entry has no prefix.
        "traversal-identity-create-address": (
            "operation 0: element identity '../../../.ssh/authorized_keys' is not a single path segment: "
            "'.' and '..' are refused, and so is any value carrying '/', '\\\\' or NUL"
        ),
        "traversal-identity-read-address": (
            "element identity '../../etc/passwd' is not a single path segment: '.' and '..' are refused, and "
            "so is any value carrying '/', '\\\\' or NUL"
        ),
        "envelope-storage-path-as-address": (
            "an address must be an object, not str; no request field takes a storage path"
        ),
    }

    @pytest.mark.parametrize("vector_name", sorted(MESSAGES))
    def test_the_refusal_text_is_unchanged(self, vector_name: str, tmp_path: Path) -> None:
        vector = fixtures.VECTORS[vector_name]
        project = fixtures.build_fixture(tmp_path / "project", vector_name)
        fixtures.check_preconditions(project, vector)
        request = fixtures.prepare_request(vector, fixtures.current_baseline_token(project))

        response = fixtures.dispatch(
            request,
            project=project,
            approved_scope=fixtures.approved_scope(vector),
            review=fixtures.host_review(vector),
        )

        assert response["message"] == self.MESSAGES[vector_name]

    def test_every_pinned_vector_is_a_shipped_negative(self) -> None:
        """The class cannot pin a message no vector produces."""
        for name in self.MESSAGES:
            assert fixtures.VECTORS[name]["kind"] == "negative", name

    def test_the_placement_refusal_is_the_kernels_own_answer(self) -> None:
        """One rule, two spellings, asserted to be one string.

        ``reg.segment_refusal`` delegates to the kernel's; a restatement breaks the first equality.
        A 250-byte group is legal as a placement value (``directory=True``) and illegal as an
        identity, so a delegation that dropped the flag fails the second half.
        """
        from swcraft_tools import model_registry as kernel
        from swcraft_tools.editing import registry as reg

        traversal = "../../../../tmp/evil"
        assert (
            reg.segment_refusal(traversal, "swc group", directory=True)
            == self.MESSAGES["traversal-placement-value-create-payload"]
        )
        assert reg.segment_refusal(traversal, "swc group", directory=True) == kernel.segment_refusal(
            traversal, "swc group", directory=True
        )

        long_group = "g" * 250
        assert reg.segment_refusal(long_group, "swc group", directory=True) is None
        assert reg.segment_refusal(long_group, "swc identity") is not None


def test_the_baseline_token_relation_runs_on_one_worker_so_its_registry_is_built_once() -> None:
    """The xdist group holds ``registry``'s class on one worker, and no consumer outside it builds it again."""
    marks = getattr(TestTheBaselineTokenIsAFunctionOfTheModelState, "pytestmark", [])
    assert [(mark.name, mark.args) for mark in marks if mark.name == "xdist_group"] == [
        ("xdist_group", ("editing-vectors-baseline",))
    ]
    assert classes_consuming(Path(__file__), "registry") == {"TestTheBaselineTokenIsAFunctionOfTheModelState"}

"""Answers bounded by the question, never by the model.

The forms tested here run on the agent task benchmark's bench: ``find`` (by name, by ``text`` and
``for_requirement``), the nearest names a ``broken-reference`` carries, ``remove`` ``with_dependents``,
``confirm_links``, the delta's ``origin`` rule and ``form: "count"``. Where the benchmark has a checker
or a reference rule, the answer is decided by it; ``for_requirement`` is held to ``trace_search.rank``
under ``run_trace``'s own inputs. The bounded model map is tested in ``test_read_batch_and_map.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import swcraft_tools.editing.engine as engine
import swcraft_tools.editing.registry as reg
import yaml
from conftest import AGENT_TASK_BENCH, agent_task_check_module, copy_agent_task_bench
from editing_vector_fixtures import response_validator
from swcraft_tools.auditor import AuditLog
from swcraft_tools.editing.address import Address, FieldStep, MemberStep
from swcraft_tools.editing.audit import AUDIT_FILENAME, CHANGE_SET_STAGED
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.delta import summarize_response
from swcraft_tools.editing.diagnosis import BROKEN_REFERENCE, RULE_CODES
from swcraft_tools.editing.engine import ChangeSetProposal, _mark_server_entries, handle_request
from swcraft_tools.editing.protocol import (
    FIND_CANDIDATES_DEFAULT,
    FIND_CANDIDATES_MAX,
    FIND_CANDIDATES_TOTAL,
    FIND_EXISTING_LINKS_MAX,
    FIND_MAX,
    FIND_TEXT_MAX,
    CountRequest,
    CreateOperation,
    DeltaEntry,
    EnvelopeError,
    FindCandidatesResponse,
    RemoveOperation,
    UpdateOperation,
    candidates_per_name,
    parse_response,
)
from swcraft_tools.editing.read import HINT_PAGE_SIZE, RESPONSE_BUDGET_WIRE_BYTES, measure
from swcraft_tools.editing.resolve import (
    CANDIDATES_PER_NAME,
    MEMBER_COLLECTIONS,
    REFERENCE_POOLS,
    UNSUGGESTED_GROUPS,
    _scored_tier,
    _word_pairing,
    _words,
)
from swcraft_tools.editing.trace_candidates import links_read, project_ranking
from swcraft_tools.model_state import load_model_state
from swcraft_tools.project import require_project
from swcraft_tools.trace_records import read_decisions, rejected_pairs
from swcraft_tools.trace_search import (
    ExistingLink,
    Ranking,
    TargetAddress,
    TraceConfig,
    build_index,
    project_name_words,
    rank,
    score_figure,
)
from swcraft_tools.yaml_load import load_yaml_document


@pytest.fixture
def bench(tmp_path: Path) -> Path:
    return copy_agent_task_bench(tmp_path)


def _tier(query: str, name: str) -> str | None:
    """The best tier at which *query* names *name*, or ``None`` (``_scored_tier`` less its closeness)."""
    found = _scored_tier(query, name)
    return None if found is None else found[0]


def _baseline(project: Path) -> str:
    return handle_request({"operation": "discover", "form": "baseline"}, project=project)["baseline"]


def _approve(proposal: ChangeSetProposal) -> ApprovalDecision:
    return ApprovalDecision(change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True)


def _approve_with_safety(proposal: ChangeSetProposal) -> ApprovalDecision:
    """Both answers: the removal of a rated component is a safety weakening the human approves apart."""
    return ApprovalDecision(
        change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=True
    )


def _find(project: Path, *names: str) -> list[dict[str, Any]]:
    answer = handle_request({"operation": "find", "names": list(names)}, project=project)
    assert answer["response"] == "find"
    assert answer["baseline"] == _baseline(project)
    return answer["results"]


def _candidate_names(project: Path) -> list[str]:
    """Every name the resolver searches on *project*, once per candidate (an element and a member may share one)."""
    registry = reg.current_registry()
    state = load_model_state(project / "model")
    names: list[str] = []
    for layer, elements in state.layers.items():
        identity = registry.layer(layer).identity_field
        for element in elements:
            names.append(getattr(element, identity))
            for field, _ in MEMBER_COLLECTIONS.get(layer, ()):
                names.extend(getattr(member, "name", None) for member in getattr(element, field, None) or ())
    return [name for name in names if isinstance(name, str)]


# find


class TestFind:
    def test_the_baseline_is_taken_before_the_snapshot_is_loaded(
        self, bench: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A change landing between the two must leave the token older than the names."""
        calls: list[str] = []
        real_token, real_load = engine.baseline_token, engine.load_snapshot

        def token(model_root: Any) -> str:
            calls.append("baseline")
            return real_token(model_root)

        def load(model_root: str) -> Any:
            calls.append("load")
            return real_load(model_root)

        monkeypatch.setattr(engine, "baseline_token", token)
        monkeypatch.setattr(engine, "load_snapshot", load)
        answer = handle_request({"operation": "find", "names": ["BrakeController"]}, project=bench)
        assert answer["response"] == "find"
        assert calls == ["baseline", "load"]

    def test_an_edit_between_the_token_and_the_load_makes_the_token_stale(
        self, bench: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_load = engine.load_snapshot

        def load_after_an_edit(model_root: str) -> Any:
            target = bench / "model" / "swc" / "application" / "DiagnosticManager.yaml"
            text = target.read_text(encoding="utf-8")
            target.write_text(text + "description: edited meanwhile\n", encoding="utf-8", newline="\n")
            return real_load(model_root)

        monkeypatch.setattr(engine, "load_snapshot", load_after_an_edit)
        found = handle_request({"operation": "find", "names": ["BrakeController"]}, project=bench)
        monkeypatch.undo()
        assert found["baseline"] != _baseline(bench)

    def test_a_near_spelling_ranks_the_closest_name_first(self, bench: Path) -> None:
        (entry,) = _find(bench, "DiagnosticManagr")
        assert entry["candidates"][0]["address"] == {"layer": "swc", "element": "DiagnosticManager"}
        assert entry["candidates"][0]["tier"] == "near"

    def test_each_tier_finds_the_component(self, bench: Path) -> None:
        exact, case, word, near = _find(
            bench, "BrakeController", "brakecontroller", "brake controller", "BrakeControlr"
        )
        for entry, tier in ((exact, "exact"), (case, "case"), (word, "word"), (near, "near")):
            first = entry["candidates"][0]
            assert first["address"] == {"layer": "swc", "element": "BrakeController"}
            assert first["tier"] == tier
            assert first["identity"].startswith("swc BrakeController (application,")

    def test_a_better_tier_ranks_first_and_an_element_before_a_member(self, bench: Path) -> None:
        (entry,) = _find(bench, "InstrumentCluster")
        tiers = [candidate["tier"] for candidate in entry["candidates"]]
        assert tiers == sorted(tiers, key=["exact", "case", "word", "near"].index)
        assert entry["candidates"][0]["address"] == {"layer": "swc", "element": "InstrumentCluster"}
        assert entry["candidates"][1]["address"]["path"] == [{"field": "members"}, {"member": "InstrumentCluster"}]

    def test_a_member_name_answers_its_owner_and_a_replayable_address(self, bench: Path) -> None:
        (entry,) = _find(bench, "rOdometer")
        candidate = entry["candidates"][0]
        assert candidate["tier"] == "exact"
        address = candidate["address"]
        assert address == {
            "layer": "swc",
            "element": "InstrumentCluster",
            "path": [{"field": "ports"}, {"member": "rOdometer"}],
        }
        assert candidate["identity"] == "port rOdometer of swc InstrumentCluster (require, IOdometer)"
        read = handle_request({"operation": "read", "address": address}, project=bench)
        assert read["value"]["interface"] == "IOdometer"

    def test_at_most_five_candidates_and_the_rest_counted(self, bench: Path) -> None:
        (entry,) = _find(bench, "Brake")
        assert len(entry["candidates"]) == CANDIDATES_PER_NAME
        matching = [name for name in _candidate_names(bench) if _tier("Brake", name) is not None]
        assert entry["more"] == len(matching) - CANDIDATES_PER_NAME

    def test_a_name_nothing_matches_is_none(self, bench: Path) -> None:
        assert _find(bench, "Zebra") == [{"name": "Zebra", "candidates": "none"}]

    @pytest.mark.parametrize(
        "names",
        [[], [""], ["  "], ["x"] * (FIND_MAX + 1), [3]],
        ids=["empty", "blank", "whitespace-only", "seventeen", "not-a-string"],
    )
    def test_a_malformed_find_is_a_schema_violation(self, bench: Path, names: list[Any]) -> None:
        assert handle_request({"operation": "find", "names": names}, project=bench)["error"] == "schema-violation"

    def test_find_takes_no_other_key(self, bench: Path) -> None:
        request = {"operation": "find", "names": ["Brake"], "form": "index"}
        assert handle_request(request, project=bench)["error"] == "schema-violation"

    def test_a_read_with_names_is_refused_as_an_unknown_key(self, bench: Path) -> None:
        """The prototype's ``read`` ``names`` never ships: the search is ``find``'s."""
        answer = handle_request({"operation": "read", "names": ["Brake"]}, project=bench)
        assert answer["error"] == "schema-violation"
        assert "names" in answer["message"]

    def test_two_names_answer_in_order_with_their_addresses_tiers_and_identities(self, bench: Path) -> None:
        answer = handle_request({"operation": "find", "names": ["brake controller", "rOdometer"]}, project=bench)
        assert set(answer) == {"response", "baseline", "results"}
        controller, odometer = answer["results"]
        assert controller["name"] == "brake controller"
        assert odometer["name"] == "rOdometer"
        for result in (controller, odometer):
            assert set(result) <= {"name", "candidates", "more"}
            for candidate in result["candidates"]:
                assert set(candidate) == {"address", "tier", "identity"}
                assert len(candidate["identity"]) <= 72
                assert "/" not in candidate["identity"]
        assert controller["candidates"][0] == {
            "address": {"layer": "swc", "element": "BrakeController"},
            "tier": "word",
            "identity": controller["candidates"][0]["identity"],
        }
        assert odometer["candidates"][0]["tier"] == "exact"

    def test_a_long_identity_line_is_cut_at_72_characters(self, bench: Path) -> None:
        component = bench / "model" / "swc" / "application" / "BrakeController.yaml"
        text = component.read_text(encoding="utf-8")
        assert text.startswith("name: BrakeController\n")
        description = "Decides the brake request from the pedal, the temperature and the protection state"
        component.write_text(
            text.replace("name: BrakeController\n", f"name: BrakeController\ndescription: {description}\n", 1),
            encoding="utf-8",
            newline="\n",
        )
        (entry,) = _find(bench, "BrakeController")
        line = f"swc BrakeController (application, 3 ports, 1 runnable) — {description}"
        assert entry["candidates"][0]["identity"] == line[:71] + "…"
        assert len(entry["candidates"][0]["identity"]) == 72

    def test_a_find_over_an_unparseable_model_file_is_a_consistency_violation(self, bench: Path) -> None:
        (bench / "model" / "hardware" / "Broken.yaml").write_text("name: [\n", encoding="utf-8", newline="\n")
        answer = handle_request({"operation": "find", "names": ["Brake"]}, project=bench)
        assert answer["error"] == "consistency-violation"
        assert "'hardware' element 'Broken'" in answer["message"]
        assert ".yaml" not in answer["message"]

    def test_an_answer_over_the_response_budget_is_refused(self, bench: Path) -> None:
        """Each entry echoes its name and nothing bounds a name's length."""
        answer = handle_request({"operation": "find", "names": ["A" * 90_000]}, project=bench)
        assert answer["error"] == "over-budget"
        assert answer["measured"]["wire_bytes"] > RESPONSE_BUDGET_WIRE_BYTES
        assert answer["measured"]["elements"] == 1
        assert "addresses" not in answer and "hints" not in answer
        assert measure(answer) < 1_000


#: Sixteen names on the agent task bench, the first six matching more than five candidates each, so every
#: per-name figure below cuts a longer answer; two exact hits that also match a second candidate and one
#: name nothing matches among them.
SIXTEEN_NAMES = (
    "Brake",
    "Temperature",
    "Protection",
    "Request",
    "Brk",
    "Brake request",
    "BrakeController",
    "rOdometer",
    "Zebra",
    "Odometer",
    "speed",
    "Controller",
    "Sensor",
    "Cluster",
    "Wheel",
    "s",
)


class TestFindIsBoundedByTheRequest:
    """``find`` answers at most sixteen candidates in all: fewer per name above three names."""

    def test_the_whole_table_from_one_to_sixteen_names(self) -> None:
        """Every request size, against a literal table and against the sixteen-in-all bound."""
        table = [candidates_per_name(n) for n in range(1, FIND_MAX + 1)]

        assert table == [5, 5, 5, 4, 3, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1]
        assert all(n * candidates_per_name(n) <= FIND_CANDIDATES_TOTAL for n in range(1, FIND_MAX + 1))

    def test_the_parser_and_the_schema_do_not_check_sixteen_in_all(self) -> None:
        """The stated limit: a response of twenty candidates, five a name, is accepted by the schema and the parser.

        The sixteen-in-all bound is the server's (``resolve_names``); this test fails once either checks the
        total, so the documents' "not checked by the schema or the parser" must change.
        """
        candidate = {
            "address": {"layer": "swc", "element": "ExampleSWC"},
            "tier": "exact",
            "identity": "swc ExampleSWC",
        }
        response = {
            "response": "find",
            "baseline": "rev1:9f4c2a7be3d18056",
            "results": [{"name": f"N{i}", "candidates": [candidate] * CANDIDATES_PER_NAME} for i in range(4)],
        }
        assert sum(len(entry["candidates"]) for entry in response["results"]) > FIND_CANDIDATES_TOTAL

        assert list(response_validator().iter_errors(response)) == []
        parse_response(response)

    def test_the_bench_names_cut_a_longer_answer(self, bench: Path) -> None:
        """The fixture is load-bearing: the first names match more than five candidates each."""
        for entry in _find(bench, *SIXTEEN_NAMES[:3]) + _find(bench, *SIXTEEN_NAMES[3:6]):
            assert len(entry["candidates"]) == CANDIDATES_PER_NAME
            assert entry["more"] > 0

    @pytest.mark.parametrize("count", [1, 3, 4, 5, 8, 9, 16])
    def test_each_name_answers_the_prefix_of_its_own_answer_and_more_counts_the_rest(
        self, bench: Path, count: int
    ) -> None:
        names = SIXTEEN_NAMES[:count]
        per_name = candidates_per_name(count)
        answered = _find(bench, *names)
        assert [entry["name"] for entry in answered] == list(names)
        total = 0
        for entry in answered:
            (alone,) = _find(bench, entry["name"])
            if alone["candidates"] == "none":
                assert entry == alone
                continue
            matched = len(alone["candidates"]) + alone.get("more", 0)
            assert entry["candidates"] == alone["candidates"][:per_name]
            assert entry.get("more", 0) == matched - len(entry["candidates"])
            assert ("more" in entry) == (matched > per_name)
            total += len(entry["candidates"])
        assert total <= FIND_CANDIDATES_TOTAL

    def test_an_exact_hit_is_first_at_sixteen_names(self, bench: Path) -> None:
        answered = {entry["name"]: entry for entry in _find(bench, *SIXTEEN_NAMES)}
        for name in ("BrakeController", "rOdometer"):
            (candidate,) = answered[name]["candidates"]
            assert candidate["tier"] == "exact"
            assert f" {name} " in f" {candidate['identity']}"
            assert answered[name]["more"] == 1
        assert answered["Zebra"] == {"name": "Zebra", "candidates": "none"}


def _pairs_by_search(query_words: list[str], name_words: list[str]) -> bool:
    """An independent oracle of :func:`_word_pairing`'s existence: every injective assignment tried."""
    from itertools import permutations

    return any(
        all(name_words[index].startswith(query) for query, index in zip(query_words, chosen, strict=True))
        for chosen in permutations(range(len(name_words)), len(query_words))
    )


@pytest.mark.parametrize(
    ("query_words", "name_words"),
    [
        (["br", "brake"], ["brake", "bracket"]),
        (["brake", "br"], ["brake", "bracket"]),
        (["b", "br", "bra"], ["bra", "brb", "b"]),
        (["b", "b", "bx"], ["bx", "by", "c"]),
        (["a", "ab", "abc", "b"], ["abc", "ab", "a", "b"]),
        (["ab", "ab", "a"], ["abc", "abd", "ax"]),
        (["ab", "ab", "a"], ["abc", "abd", "bx"]),
    ],
)
def test_the_pairing_finds_a_matching_exactly_when_one_exists(query_words: list[str], name_words: list[str]) -> None:
    """Fewest-words-first over the nested-or-disjoint prefix sets agrees with an exhaustive search."""
    paired = _word_pairing(query_words, name_words)
    assert (paired is not None) == _pairs_by_search(query_words, name_words)
    if paired is not None:
        assert all(word.startswith(query) for query, word in zip(query_words, paired, strict=True))


def _find_text(project: Path, text: str) -> dict[str, Any]:
    answer = handle_request({"operation": "find", "text": text}, project=project)
    assert answer["response"] == "find", answer
    assert answer["baseline"] == _baseline(project)
    assert answer["text"] == text
    return answer


def _found_ids(answer: dict[str, Any]) -> list[str]:
    candidates = answer["candidates"]
    return [] if candidates == "none" else [candidate["address"]["element"] for candidate in candidates]


class TestFindText:
    """``find`` by ``text``: the requirements whose text holds every word of a phrase."""

    def test_brake_temperature_answers_question_21s_twelve_and_nothing_else(self, bench: Path) -> None:
        """The server's word rule and the bench's adjacent-phrase rule agree on the small bench: the same twelve.

        The rules could differ (a text holding ``brake`` and ``temperature`` apart); this pins that they do not.
        """
        task = json.loads(
            (AGENT_TASK_BENCH.parent / "tasks" / "21-requirements-about-a-topic.json").read_text(encoding="utf-8")
        )
        answer = _find_text(bench, "brake temperature")
        assert set(answer) == {"response", "baseline", "text", "candidates"}
        found = _found_ids(answer)
        assert found == sorted(task["expect"]["answer"]["mentions_all"])
        assert not set(found) & set(task["expect"]["answer"]["mentions_none"])
        for candidate in answer["candidates"]:
            assert set(candidate) == {"address", "identity", "words"}
            assert candidate["address"]["layer"] == "requirements"
            assert set(candidate["address"]) == {"layer", "element"}
            assert candidate["identity"].startswith(candidate["address"]["element"])
            assert len(candidate["identity"]) <= 72
            assert candidate["words"] == ["brake", "temperature"]

    def test_element_names_never_take_the_places(self, bench: Path) -> None:
        """The name form's ``word`` tier fills its five slots with elements; the text form answers requirements."""
        (named,) = _find(bench, "brake temperature")
        assert {candidate["address"]["layer"] for candidate in named["candidates"]} != {"requirements"}
        found = _find_text(bench, "brake temperature")["candidates"]
        assert {candidate["address"]["layer"] for candidate in found} == {"requirements"}

    def test_one_word_answers_every_requirement_holding_it_or_a_word_it_starts(self, bench: Path) -> None:
        answer = _find_text(bench, "therm")
        assert all(word.startswith("therm") for c in answer["candidates"] for word in c["words"])
        state = load_model_state(bench / "model")
        holding = sorted(
            requirement.id
            for requirement in state.layers["requirements"]
            if re.search(r"\btherm", f"{requirement.title or ''} {requirement.description or ''}", re.IGNORECASE)
        )
        assert holding, "the bench holds requirements about thermal protection"
        assert set(holding) <= set(_found_ids(answer))

    def test_an_id_finds_its_requirement(self, bench: Path) -> None:
        answer = _find_text(bench, "SWR-THP-007")
        assert _found_ids(answer) == ["SWR-THP-007"]
        assert answer["candidates"][0]["identity"] == "SWR-THP-007 — Brake temperature range"
        assert answer["candidates"][0]["words"] == ["swr", "thp", "007"]

    def test_the_answer_is_bounded_in_id_order_and_more_counts_the_rest(self, bench: Path) -> None:
        state = load_model_state(bench / "model")
        every = sorted(
            requirement.id for requirement in state.layers["requirements"] if requirement.id.startswith("SWR-")
        )
        assert len(every) > FIND_TEXT_MAX
        answer = _find_text(bench, "swr")
        assert _found_ids(answer) == every[:FIND_TEXT_MAX]
        assert answer["more"] == len(every) - FIND_TEXT_MAX

    def test_a_stub_is_found_by_its_id_and_the_text_it_carries(self, bench: Path) -> None:
        assert _found_ids(_find_text(bench, "HAZ_BTP_BRAKE_FADE")) == ["HAZ_BTP_BRAKE_FADE"]
        assert _found_ids(_find_text(bench, "placeholder")) == ["HAZ_BTP_BRAKE_FADE"]

    def test_a_repeated_query_word_needs_two_words_of_the_text(self, bench: Path) -> None:
        requirements = bench / "model" / "requirements"
        for identifier, title, description in (
            ("REQ_TEST_ONCE", "Quokka lamp", "The lamp shall glow."),
            ("REQ_TEST_TWICE", "Quokka wombat", "The quokka shall glow."),
        ):
            (requirements / f"{identifier}.yaml").write_text(
                f"id: {identifier}\ntitle: {title}\ndescription: {description}\n"
                "req_type: req\nversion: 1\nsource: architecture\n",
                encoding="utf-8",
                newline="\n",
            )
        assert _found_ids(_find_text(bench, "quokka")) == ["REQ_TEST_ONCE", "REQ_TEST_TWICE"]
        assert _found_ids(_find_text(bench, "quokka quokka")) == ["REQ_TEST_TWICE"]
        assert _find_text(bench, "quokka quokka quokka")["candidates"] == "none"

    def test_a_thousand_word_query_over_a_thousand_word_text_is_answered(self, bench: Path) -> None:
        """No recursion whose depth grows with the query: the pairing is one pass."""
        (bench / "model" / "requirements" / "REQ_TEST_LONG.yaml").write_text(
            "id: REQ_TEST_LONG\ntitle: Long\ndescription: " + " ".join(["quokka"] * 1000) + "\n"
            "req_type: req\nversion: 1\nsource: architecture\n",
            encoding="utf-8",
            newline="\n",
        )
        assert _found_ids(_find_text(bench, " ".join(["quokka"] * 1000))) == ["REQ_TEST_LONG"]
        assert _find_text(bench, " ".join(["quokka"] * 1001))["candidates"] == "none"

    def test_an_id_two_files_declare_answers_once_per_file(self, bench: Path) -> None:
        requirements = bench / "model" / "requirements"
        text = (requirements / "SWR-THP-007.yaml").read_text(encoding="utf-8")
        (requirements / "SWR-THP-007~1.yaml").write_text(text, encoding="utf-8", newline="\n")
        assert _found_ids(_find_text(bench, "SWR-THP-007")) == ["SWR-THP-007", "SWR-THP-007"]

    def test_nothing_matching_answers_none(self, bench: Path) -> None:
        answer = _find_text(bench, "brake zebra")
        assert answer["candidates"] == "none"
        assert "more" not in answer

    def test_the_answer_never_carries_the_document_field(self, bench: Path) -> None:
        path = bench / "model" / "requirements" / "SWR-THP-007.yaml"
        text = path.read_text(encoding="utf-8")
        path.write_text(text + "document: specs/thermal/brake-requirements.reqif\n", encoding="utf-8", newline="\n")
        answer = _find_text(bench, "brake temperature range")
        assert "SWR-THP-007" in _found_ids(answer)
        wire = json.dumps(answer)
        assert "reqif" not in wire and "specs/" not in wire

    @pytest.mark.parametrize(
        "request_",
        [
            {"operation": "find", "names": ["Brake"], "text": "brake"},
            {"operation": "find"},
            {"operation": "find", "text": ""},
            {"operation": "find", "text": "   "},
            {"operation": "find", "text": 3},
            {"operation": "find", "text": "brake", "form": "index"},
        ],
        ids=["names-and-text", "neither", "empty", "whitespace-only", "not-a-string", "another-key"],
    )
    def test_a_malformed_text_find_is_a_schema_violation(self, bench: Path, request_: dict[str, Any]) -> None:
        answer = handle_request(request_, project=bench)
        assert answer["error"] == "schema-violation", answer

    def test_names_and_text_together_name_both_forms(self, bench: Path) -> None:
        answer = handle_request({"operation": "find", "names": ["Brake"], "text": "brake"}, project=bench)
        assert answer["message"] == (
            "a find request carries exactly one of ['names', 'text', 'for_requirement'], not ['names', 'text']"
        )

    def test_an_answer_over_the_response_budget_is_refused(self, bench: Path) -> None:
        """The phrase is echoed, and nothing bounds its length."""
        answer = handle_request({"operation": "find", "text": "brake " + "A" * 90_000}, project=bench)
        assert answer["error"] == "over-budget"
        assert answer["measured"]["wire_bytes"] > RESPONSE_BUDGET_WIRE_BYTES
        assert measure(answer) < 1_000


def _find_for(project: Path, requirement: str, candidates: int | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"operation": "find", "for_requirement": requirement}
    if candidates is not None:
        request["candidates"] = candidates
    return handle_request(request, project=project)


#: A search target's kind -> the layer of the element holding its ``satisfies`` list: the oracle's own table.
_HOLDER_LAYERS = {
    "swc": "swc",
    "port": "swc",
    "runnable": "swc",
    "interface": "interfaces",
    "composition": "compositions",
    "task": "tasks",
    "processor": "hardware",
    "core": "hardware",
    "memory": "hardware",
    "partition": "hardware",
}


def _holder(target: Any) -> dict[str, Any]:
    """The protocol address of the element or member *target* names, spelled by the oracle itself."""
    layer = _HOLDER_LAYERS[target.kind]
    if target.owner is None:
        return Address(layer=layer, element=target.name).to_json()
    steps = (FieldStep(f"{target.kind}s"), MemberStep(target.name))
    return Address(layer=layer, element=target.owner, path=steps).to_json()


def _ranked_as_the_experiment(
    project: Path, requirement: str, candidates: int, *, name_words: bool = True
) -> dict[str, Any]:
    """The oracle: ``trace_search.rank`` under the inputs ``run_trace`` passes, written out field by field.

    The model is read by ``load_model_state`` as ``_prepare`` reads it, not the engine's snapshot, and the
    wire shape is built from the :class:`~swcraft_tools.trace_search.Ranking`, never through
    ``trace_candidates``' projection, the code under test.
    """
    loaded = require_project(str(project / "swcraft.yaml"))
    state = load_model_state(loaded.resolved_model_root)
    words = project_name_words(loaded) if name_words else frozenset()
    index = build_index(state, TraceConfig(), rejected_pairs(read_decisions(loaded)), words)
    ranking = rank(index, index.requirements[requirement], candidates)
    places = [
        {
            "group": {"kind": place.group.kind, "name": place.group.name},
            "score": score_figure(place.score),
            "evidence": [dict(record) for record in place.evidence],
            "rows": [
                {
                    "address": _holder(row.address),
                    "target": {"kind": row.address.kind, "owner": row.address.owner, "name": row.address.name},
                    "score": score_figure(row.score),
                    "band": row.band,
                    "evidence": [dict(record) for record in row.evidence],
                }
                for row in place.rows
            ],
        }
        for place in ranking.ranked[:candidates]
    ]
    links = [
        {
            "address": _holder(link.target),
            "target": {"kind": link.target.kind, "owner": link.target.owner, "name": link.target.name},
            "member": link.member,
            "version": link.version,
            "confirmed": link.confirmed,
        }
        for link in index.links.get(requirement, ())
    ]
    return {"neighbours": ranking.neighbours, "places": places or "none", "existing_links": links}


def _ranking_part(answer: dict[str, Any]) -> dict[str, Any]:
    return {key: answer[key] for key in ("neighbours", "places", "existing_links")}


def _group_names(answer: dict[str, Any]) -> list[str]:
    places = answer["places"]
    return [] if places == "none" else [place["group"]["name"] for place in places]


def _text_terms(place: dict[str, Any]) -> list[str]:
    return [term for record in place["evidence"] if record["kind"] == "text" for term in record["terms"]]


class TestFindForRequirement:
    """``find`` with ``for_requirement``: the trace search's places one to one, in its order.

    The oracle is the search itself, called as ``run_trace`` calls it (:func:`_ranked_as_the_experiment`).
    """

    def test_the_places_are_the_searchs_under_run_traces_inputs(self, bench: Path) -> None:
        answer = _find_for(bench, "REQ_BTP_CORE_LOAD")
        assert set(answer) == {"response", "baseline", "for_requirement", "neighbours", "places", "existing_links"}
        assert answer["response"] == "find"
        assert answer["baseline"] == _baseline(bench)
        assert answer["for_requirement"] == "REQ_BTP_CORE_LOAD"
        assert _ranking_part(answer) == _ranked_as_the_experiment(bench, "REQ_BTP_CORE_LOAD", FIND_CANDIDATES_DEFAULT)
        assert len(answer["places"]) == FIND_CANDIDATES_DEFAULT

    def test_question_22s_three_places_each_with_a_shared_term(self, bench: Path) -> None:
        task = json.loads(
            (AGENT_TASK_BENCH.parent / "tasks" / "22-candidates-for-a-requirement.json").read_text(encoding="utf-8")
        )
        expected = task["expect"]["answer"]["candidates"]
        answer = _find_for(bench, "REQ_BTP_CORE_LOAD")
        assert _group_names(answer) == expected["top"]
        for place in answer["places"]:
            assert set(expected["terms"][place["group"]["name"]]) <= set(_text_terms(place))
        linked = {link["target"]["name"] for link in answer["existing_links"]}
        assert linked == {entry.split(" ")[-1] for entry in task["reference"]["linked"]}
        assert all(link["confirmed"] for link in answer["existing_links"])

    def test_a_row_carries_the_address_a_change_set_takes_and_the_searchs_target(self, bench: Path) -> None:
        answer = _find_for(bench, "SWR-THP-007", FIND_CANDIDATES_MAX)
        rows = [row for place in answer["places"] for row in place["rows"]]
        assert rows
        for row in rows:
            target, address = row["target"], row["address"]
            assert row["band"] in ("strong", "medium", "weak")
            assert row["score"]["d"] == 1_000_000
            if target["owner"] is None:
                assert address == {"layer": address["layer"], "element": target["name"]}
            else:
                field = {"port": "ports", "runnable": "runnables"}[target["kind"]]
                assert address == {
                    "layer": "swc",
                    "element": target["owner"],
                    "path": [{"field": field}, {"member": target["name"]}],
                }
            read = handle_request({"operation": "read", "address": address}, project=bench)
            assert read["response"] == "read", read

    @pytest.mark.parametrize("candidates", [1, FIND_CANDIDATES_MAX])
    def test_candidates_bounds_the_places_as_the_search_cuts_them(self, bench: Path, candidates: int) -> None:
        answer = _find_for(bench, "SWR-THP-007", candidates)
        assert _ranking_part(answer) == _ranked_as_the_experiment(bench, "SWR-THP-007", candidates)
        assert 1 <= len(answer["places"]) <= candidates

    def test_a_rejected_target_is_never_a_row(self, bench: Path) -> None:
        """A reject planted in the copy's ``trace-decisions.yaml`` at the current version takes the target out."""
        before = _find_for(bench, "REQ_BTP_CORE_LOAD")
        assert _group_names(before)[0] == "P_Brake"
        decisions = bench / "trace-decisions.yaml"
        text = decisions.read_text(encoding="utf-8")
        planted = (
            "- requirement: REQ_BTP_CORE_LOAD\n  version: 1\n  target:\n    kind: partition\n    owner: null\n"
            "    name: P_Brake\n  decision: reject\n"
        )
        decisions.write_text(text + planted, encoding="utf-8", newline="\n")
        after = _find_for(bench, "REQ_BTP_CORE_LOAD")
        assert "P_Brake" not in _group_names(after)
        assert _ranking_part(after) == _ranked_as_the_experiment(bench, "REQ_BTP_CORE_LOAD", FIND_CANDIDATES_DEFAULT)

    def test_the_project_name_words_are_dropped_as_the_experiment_drops_them(self, bench: Path) -> None:
        """The bench is ``vehicle-control-showcase``: "vehicle" in a requirement's text is no term."""
        (bench / "model" / "requirements" / "REQ_TEST_VEHICLE.yaml").write_text(
            "id: REQ_TEST_VEHICLE\ntitle: Vehicle state broadcast\n"
            "description: The vehicle shall broadcast its vehicle speed state.\n"
            "req_type: req\nversion: 1\nsource: architecture\n",
            encoding="utf-8",
            newline="\n",
        )
        answer = _find_for(bench, "REQ_TEST_VEHICLE")
        assert _ranking_part(answer) == _ranked_as_the_experiment(bench, "REQ_TEST_VEHICLE", FIND_CANDIDATES_DEFAULT)
        assert all("vehicle" not in _text_terms(place) for place in answer["places"])
        with_the_word = _ranked_as_the_experiment(bench, "REQ_TEST_VEHICLE", FIND_CANDIDATES_DEFAULT, name_words=False)
        assert any("vehicle" in _text_terms(place) for place in with_the_word["places"])
        assert with_the_word["places"] != answer["places"]

    def test_a_requirement_sharing_no_term_with_any_element_answers_none(self, bench: Path) -> None:
        (bench / "model" / "requirements" / "REQ_TEST_NOTHING.yaml").write_text(
            "id: REQ_TEST_NOTHING\ntitle: Quokka xylophone\n"
            "description: Zeppelin marmalade quibbles over a lighthouse.\n"
            "req_type: req\nversion: 1\nsource: architecture\n",
            encoding="utf-8",
            newline="\n",
        )
        answer = _find_for(bench, "REQ_TEST_NOTHING")
        assert answer["response"] == "find", answer
        assert answer["places"] == "none"
        assert answer["existing_links"] == []
        assert FindCandidatesResponse.from_json(answer).to_json() == answer
        assert _ranking_part(answer) == _ranked_as_the_experiment(bench, "REQ_TEST_NOTHING", FIND_CANDIDATES_DEFAULT)

    def test_an_unknown_requirement_is_refused_naming_the_nearest_ids(self, bench: Path) -> None:
        answer = _find_for(bench, "REQ_BTP_CORE_LOAF")
        assert answer["error"] == "broken-reference", answer
        assert answer["addresses"] == [{"layer": "requirements", "element": "REQ_BTP_CORE_LOAF"}]
        assert "nearest: REQ_BTP_CORE_LOAD" in answer["message"]
        assert str(bench) not in json.dumps(answer)

    def test_a_stub_is_refused(self, bench: Path) -> None:
        answer = _find_for(bench, "HAZ_BTP_BRAKE_FADE")
        assert answer["error"] == "schema-violation", answer
        assert answer["addresses"] == [{"layer": "requirements", "element": "HAZ_BTP_BRAKE_FADE"}]
        assert "stub" in answer["message"]

    def test_an_ambiguous_identity_is_refused_as_the_experiment_refuses_it(self, bench: Path) -> None:
        requirements = bench / "model" / "requirements"
        text = (requirements / "SWR-THP-007.yaml").read_text(encoding="utf-8")
        (requirements / "SWR-THP-007-copy.yaml").write_text(text, encoding="utf-8", newline="\n")
        answer = _find_for(bench, "REQ_BTP_CORE_LOAD")
        assert answer["error"] == "consistency-violation", answer
        assert "addresses" not in answer
        assert "ambiguous" in answer["message"] and "name.duplicate-requirement" in answer["message"]
        assert str(bench) not in json.dumps(answer)

    def test_an_unreadable_decisions_file_is_refused_by_its_role_never_its_path(self, bench: Path) -> None:
        (bench / "trace-decisions.yaml").write_text("formatVersion: [\n", encoding="utf-8", newline="\n")
        answer = _find_for(bench, "REQ_BTP_CORE_LOAD")
        assert answer["error"] == "consistency-violation", answer
        assert "addresses" not in answer
        assert "trace decisions file cannot be read" in answer["message"]
        wire = json.dumps(answer)
        assert str(bench) not in wire and "trace-decisions.yaml" not in wire

    @pytest.mark.parametrize("where", ["from", "key"])
    def test_a_decision_record_quoting_a_location_is_refused_by_its_record_alone(self, bench: Path, where: str) -> None:
        """The reader's sentence quotes a record's values, its pointer names a key: neither travels."""
        location = bench.as_posix()
        extra = f"  from: '{location}'\n" if where == "from" else f"  '{location}': 1\n"
        (bench / "trace-decisions.yaml").write_text(
            "formatVersion: 1\ndecisions:\n- requirement: REQ_BTP_CORE_LOAD\n  version: 1\n"
            "  target:\n    kind: partition\n    owner: null\n    name: P_Brake\n"
            f"  decision: {'confirm' if where == 'from' else 'reject'}\n{extra}",
            encoding="utf-8",
            newline="\n",
        )
        answer = _find_for(bench, "REQ_BTP_CORE_LOAD")
        assert answer["error"] == "consistency-violation", answer
        assert "(record decisions/0 is not one the format holds;" in answer["message"]
        assert "swcraft experiment trace" in answer["message"]
        assert location not in json.dumps(answer) and str(bench) not in json.dumps(answer)

    def test_a_file_that_stops_parsing_after_the_snapshot_is_refused_as_the_snapshot_refuses_it(
        self, bench: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ambiguity check's second read of the model root meets a broken file: a refusal, never a raise."""
        broken = bench / "model" / "requirements" / "SWR-THP-007.yaml"
        loaded = engine.load_snapshot

        def load_then_break(model_root: str) -> Any:
            snapshot = loaded(model_root)
            broken.write_text("id: [oops\n", encoding="utf-8", newline="\n")
            return snapshot

        monkeypatch.setattr(engine, "load_snapshot", load_then_break)
        answer = _find_for(bench, "REQ_BTP_CORE_LOAD")
        monkeypatch.setattr(engine, "load_snapshot", loaded)
        refused = handle_request({"operation": "find", "names": ["Brake"]}, project=bench)
        assert "error" in refused
        assert answer == refused

    @pytest.mark.parametrize(
        "request_",
        [
            {"operation": "find", "names": ["Brake"], "text": "brake", "for_requirement": "REQ_BTP_CORE_LOAD"},
            {"operation": "find", "text": "brake", "for_requirement": "REQ_BTP_CORE_LOAD"},
            {"operation": "find", "names": ["Brake"], "candidates": 3},
            {"operation": "find", "text": "brake", "candidates": 3},
            {"operation": "find", "candidates": 3},
            {"operation": "find", "for_requirement": ""},
            {"operation": "find", "for_requirement": 7},
            {"operation": "find", "for_requirement": "REQ_BTP_CORE_LOAD", "candidates": 0},
            {"operation": "find", "for_requirement": "REQ_BTP_CORE_LOAD", "candidates": FIND_CANDIDATES_MAX + 1},
            {"operation": "find", "for_requirement": "REQ_BTP_CORE_LOAD", "candidates": True},
            {"operation": "find", "for_requirement": "REQ_BTP_CORE_LOAD", "candidates": 2.5},
        ],
        ids=[
            "three-forms",
            "text-and-requirement",
            "candidates-with-names",
            "candidates-with-text",
            "candidates-alone",
            "empty-id",
            "not-a-string",
            "zero",
            "eleven",
            "boolean",
            "fraction",
        ],
    )
    def test_a_malformed_requirement_find_is_a_schema_violation(self, bench: Path, request_: dict[str, Any]) -> None:
        answer = handle_request(request_, project=bench)
        assert answer["error"] == "schema-violation", answer

    def test_an_integral_float_candidates_is_the_integer(self, bench: Path) -> None:
        """``candidates: 2.0`` is the integer ``2``, as the schema's ``type: integer`` reads it."""
        request_ = {"operation": "find", "for_requirement": "REQ_BTP_CORE_LOAD"}
        as_float = handle_request(request_ | {"candidates": 2.0}, project=bench)
        assert "error" not in as_float, as_float
        assert as_float == handle_request(request_ | {"candidates": 2}, project=bench)

    def test_three_forms_together_name_every_form(self, bench: Path) -> None:
        request_ = {"operation": "find", "names": ["Brake"], "text": "brake", "for_requirement": "REQ_BTP_CORE_LOAD"}
        answer = handle_request(request_, project=bench)
        assert answer["message"] == (
            "a find request carries exactly one of ['names', 'text', 'for_requirement'], "
            "not ['names', 'text', 'for_requirement']"
        )


class TestTheWordTier:
    """The word tier matches whole words or their prefixes, never a substring of the squashed name."""

    @staticmethod
    def _names(project: Path) -> set[str]:
        """Every name the resolver searches on *project*: each layer's identities and the searched members'."""
        registry = reg.current_registry()
        state = load_model_state(project / "model")
        names: set[str] = set()
        for layer, elements in state.layers.items():
            identity = registry.layer(layer).identity_field
            for element in elements:
                names.add(getattr(element, identity))
                for field, _ in MEMBER_COLLECTIONS.get(layer, ()):
                    members = getattr(element, field, None) or ()
                    names.update(getattr(member, "name", None) for member in members)
        return {name for name in names if isinstance(name, str)}

    @classmethod
    def _word_hits(cls, project: Path, query: str) -> set[str]:
        return {name for name in cls._names(project) if _tier(query, name) == "word"}

    def test_p_brake_has_no_word_hit_on_i_brake_temperature(self, bench: Path) -> None:
        """``p`` starts none of ``i``, ``brake`` and ``temperature``."""
        assert _tier("P_Brake", "IBrakeTemperature") is None
        assert "IBrakeTemperature" not in self._word_hits(bench, "P_Brake")
        (entry,) = _find(bench, "P_Brake")
        candidates = entry["candidates"] if isinstance(entry["candidates"], list) else []
        assert {"layer": "interfaces", "element": "IBrakeTemperature"} not in [c["address"] for c in candidates]

    def test_brake_controller_finds_brake_controller(self, bench: Path) -> None:
        (entry,) = _find(bench, "brake controller")
        assert entry["candidates"][0]["address"] == {"layer": "swc", "element": "BrakeController"}
        assert entry["candidates"][0]["tier"] == "word"

    def test_s_probe_finds_the_port_s_probe_by_exact(self, bench: Path) -> None:
        component = next((bench / "model" / "swc").rglob("InstrumentCluster.yaml"))
        text = component.read_text(encoding="utf-8")
        port = "- name: S_Probe\n  direction: require\n  interface: IOdometer\n"
        component.write_text(text.replace("ports:\n", "ports:\n" + port, 1), encoding="utf-8", newline="\n")
        (entry,) = _find(bench, "S_Probe")
        assert entry["candidates"][0] == {
            "address": {
                "layer": "swc",
                "element": "InstrumentCluster",
                "path": [{"field": "ports"}, {"member": "S_Probe"}],
            },
            "tier": "exact",
            "identity": "port S_Probe of swc InstrumentCluster (require, IOdometer)",
        }
        assert _tier("s_probe", "S_Probe") == "case"
        assert _tier("S Probe", "S_Probe") == "word"
        assert _tier("S_Probe", "SensorProbeTemperature") == "word"
        assert _tier("S_Probe", "Probe") not in {"exact", "case", "word"}

    def test_temp_sensor_finds_brake_temperature_sensor_by_prefix(self, bench: Path) -> None:
        assert _tier("temp sensor", "BrakeTemperatureSensor") == "word"
        assert "BrakeTemperatureSensor" in self._word_hits(bench, "temp sensor")

    def test_brake_alone_finds_every_name_with_a_word_brake_and_nothing_else(self, bench: Path) -> None:
        hits = self._word_hits(bench, "Brake")
        expected = {
            name
            for name in self._names(bench)
            if name.lower() != "brake" and any(word.startswith("brake") for word in _words(name))
        }
        assert hits == expected
        assert "IBrakeTemperature" in expected
        (entry,) = _find(bench, "Brake")
        assert [candidate["tier"] for candidate in entry["candidates"]] == ["word"] * CANDIDATES_PER_NAME
        word_candidates = [name for name in _candidate_names(bench) if name in expected]
        assert entry["more"] == len(word_candidates) - CANDIDATES_PER_NAME

    def test_each_query_word_takes_a_distinct_name_word(self) -> None:
        assert _tier("brake brake", "BrakeController") is None
        assert _tier("brake brake", "BrakeToBrake") == "word"
        assert _tier("b brake", "BrakeBus") == "word"


# broken-reference carries the nearest names


class TestBrokenReferenceNamesTheNearest:
    def test_a_read_of_a_misspelled_element(self, bench: Path) -> None:
        answer = handle_request(
            {"operation": "read", "address": {"layer": "swc", "element": "BrakeControler"}}, project=bench
        )
        assert answer["error"] == "broken-reference"
        assert "swc holds no 'BrakeControler'; nearest: BrakeController." in answer["message"]

    def test_a_miss_costs_no_second_load_of_the_model(self, bench: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The nearest names come from the snapshot the read itself loaded: two misses, two reads, two loads."""
        import swcraft_tools.editing.read as read_module

        loads: list[str] = []
        real = read_module.load_snapshot

        def counting(model_root: str) -> Any:
            loads.append(model_root)
            return real(model_root)

        monkeypatch.setattr(read_module, "load_snapshot", counting)
        monkeypatch.setattr(engine, "load_snapshot", counting)
        answer = handle_request(
            {
                "operation": "read",
                "addresses": [
                    {"layer": "swc", "element": "BrakeControler"},
                    {"layer": "swc", "element": "InstrumntCluster"},
                ],
            },
            project=bench,
        )
        first, second = answer["answers"]
        assert "nearest: BrakeController." in first["message"]
        assert "nearest: InstrumentCluster." in second["message"]
        assert len(loads) == 2

    def test_a_misspelled_member_below_a_member_is_answered(self, bench: Path) -> None:
        """The walk goes as deep as the address: an operation's argument, not only the operation."""
        path = [{"field": "operations"}, {"member": "ReadSnapshot"}, {"field": "arguments"}, {"member": "snapsho"}]
        answer = handle_request(
            {"operation": "read", "address": {"layer": "interfaces", "element": "IBrakeDiagnostics", "path": path}},
            project=bench,
        )
        assert answer["error"] == "broken-reference"
        assert answer["message"].endswith(
            "IBrakeDiagnostics.operations.ReadSnapshot.arguments holds no 'snapsho'; nearest: snapshot."
        )

    def test_a_value_type_base_is_searched_among_the_base_types(self, bench: Path) -> None:
        """``uint8`` is an implementation type, never a base: the sentence names the one layer ``base`` reads."""
        create = {
            "operation": "create",
            "address": {"layer": "implementation_types", "element": "MyValue"},
            "value": {"name": "MyValue", "kind": "value_type", "base": "uint8"},
        }
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [create]}, project=bench
        )
        assert answer["error"] == "broken-reference"
        assert answer["message"].endswith("base_types holds no 'uint8'; nearest: uint8_base.")
        assert "implementation_types holds no" not in answer["message"]

    def test_a_data_element_type_is_searched_in_the_layer_of_its_field(self, bench: Path) -> None:
        path = [{"field": "data_elements"}, {"member": "temperature"}, {"field": "application_type"}]
        update = {
            "operation": "update",
            "address": {"layer": "interfaces", "element": "IBrakeTemperature", "path": path},
            "value": "uint8",
        }
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [update]}, project=bench
        )
        assert answer["error"] == "broken-reference"
        assert "application_types holds no 'uint8'" in answer["message"]
        assert "implementation_types holds no" not in answer["message"]

    def test_the_nearest_names_carry_no_storage_name(self, bench: Path) -> None:
        answer = handle_request(
            {"operation": "read", "address": {"layer": "swc", "element": "BrakeControler"}}, project=bench
        )
        assert str(bench) not in answer["message"]
        assert ".yaml" not in answer["message"]

    def test_an_operation_at_a_misspelled_element(self, bench: Path) -> None:
        operation = {"operation": "remove", "address": {"layer": "swc", "element": "InstrumntCluster"}}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [operation]}, project=bench
        )
        assert answer["error"] == "broken-reference"
        assert "nearest: InstrumentCluster." in answer["message"]

    def test_an_operation_at_a_misspelled_member(self, bench: Path) -> None:
        path = [{"field": "ports"}, {"member": "rOdometr"}, {"field": "description"}]
        operation = {"operation": "update", "address": {"layer": "swc", "element": "InstrumentCluster", "path": path}}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [{**operation, "value": "x"}]},
            project=bench,
        )
        assert answer["error"] == "broken-reference"
        assert "InstrumentCluster.ports holds no 'rOdometr'; nearest: rOdometer." in answer["message"]

    @pytest.mark.parametrize(
        ("value", "sentence"),
        [
            (
                {"from": {"swc": "DiagnosticManagr", "port": "pBrakeDiagnostics"}},
                "swc holds no 'DiagnosticManagr'; nearest: DiagnosticManager.",
            ),
            (
                {"from": {"swc": "DiagnosticManager", "port": "pBrakeDiagnostic"}},
                "nearest: pBrakeDiagnostics",
            ),
        ],
        ids=["component", "port"],
    )
    def test_a_staged_reference_that_does_not_resolve(self, bench: Path, value: dict, sentence: str) -> None:
        connector = {"name": "C1", "to": {"swc": "InstrumentCluster", "port": "rBrakeDiagnostics"}, **value}
        operation = {"operation": "create", "address": {"layer": "connectors", "element": "C1"}, "value": connector}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [operation]}, project=bench
        )
        assert answer["error"] == "broken-reference"
        assert sentence in answer["message"]

    def test_a_port_on_another_component_is_searched_on_the_component_named(self, bench: Path) -> None:
        """``rBrakeDiagnostics`` is ``InstrumentCluster``'s: on ``DiagnosticManager`` it is missing.

        Only ``DiagnosticManager``'s ports are offered.
        """
        connector = {
            "name": "C1",
            "from": {"swc": "DiagnosticManager", "port": "rBrakeDiagnostics"},
            "to": {"swc": "InstrumentCluster", "port": "rBrakeDiagnostics"},
        }
        operation = {"operation": "create", "address": {"layer": "connectors", "element": "C1"}, "value": connector}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [operation]}, project=bench
        )
        assert answer["error"] == "broken-reference"
        assert answer["message"].endswith(
            "DiagnosticManager.ports holds no 'rBrakeDiagnostics'; nearest: pBrakeDiagnostics."
        )

    @pytest.mark.parametrize(
        ("port", "sentence"),
        [
            ("rOperatingStat", "Chassis.ports holds no 'rOperatingStat'; nearest: rOperatingState."),
            # `rOdometer` is `InstrumentCluster`'s: an outer port never resolves on a component.
            ("rOdometr", "Chassis.ports holds no 'rOdometr' and nothing near it."),
        ],
    )
    def test_a_delegations_outer_port_is_searched_on_its_own_composition(
        self, bench: Path, port: str, sentence: str
    ) -> None:
        address = {
            "layer": "compositions",
            "element": "Chassis",
            "path": [{"field": "delegations"}, {"member": "DelegateOperatingState"}, {"field": "port"}],
        }
        operation = {"operation": "update", "address": address, "value": port}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [operation]}, project=bench
        )
        assert answer["error"] == "broken-reference"
        assert answer["message"].endswith(sentence)

    @pytest.mark.parametrize(
        ("operation", "sentence"),
        [
            pytest.param(
                {
                    "operation": "update",
                    "address": {
                        "layer": "compositions",
                        "element": "Chassis",
                        "path": [
                            {"field": "delegations"},
                            {"member": "DelegateOperatingState"},
                            {"field": "member_port"},
                        ],
                    },
                    "value": "rOperatingStat",
                },
                "BrakeSubsystem.ports holds no 'rOperatingStat'; nearest: rOperatingState.",
                id="a-delegations-member-of-composition-type",
            ),
            pytest.param(
                {
                    "operation": "create",
                    "address": {"layer": "connectors", "element": "C1"},
                    "value": {
                        "name": "C1",
                        "composition": "Chassis",
                        "from": {"composition": "BrakeSubsystem", "port": "rOperatingStat"},
                        "to": {"swc": "BrakeController", "port": "rOperatingState"},
                    },
                },
                "BrakeSubsystem.ports holds no 'rOperatingStat'; nearest: rOperatingState.",
                id="a-connector-endpoint-of-composition-type",
            ),
        ],
    )
    def test_a_port_is_searched_in_the_layer_the_referring_end_declares(
        self, bench: Path, operation: dict[str, Any], sentence: str
    ) -> None:
        """Only the declared type's ports are offered when a component and a composition share a name.

        ``rOperatingStates`` is the component ``BrakeSubsystem``'s, a type neither end names."""
        twin = bench / "model" / "swc" / "basic_software" / "BrakeSubsystem.yaml"
        twin.write_text(
            "name: BrakeSubsystem\n"
            "external: true\n"
            "external_ref: /Platform/ComponentTypes/BrakeSubsystem\n"
            "external_dest: APPLICATION-SW-COMPONENT-TYPE\n"
            "ports:\n"
            "- name: rOperatingStates\n"
            "  direction: require\n"
            "  observed_interface: IVehicleOperatingState\n",
            encoding="utf-8",
            newline="\n",
        )
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [operation]}, project=bench
        )
        assert answer["error"] == "broken-reference"
        assert answer["message"].endswith(sentence)

    def test_a_read_miss_with_nothing_near_adds_no_sentence(self, bench: Path) -> None:
        answer = handle_request({"operation": "read", "address": {"layer": "swc", "element": "Zebra"}}, project=bench)
        assert answer["error"] == "broken-reference"
        assert answer["message"] == "no 'swc' element with name 'Zebra'"

    def test_a_member_identity_that_is_not_a_name_gets_no_sentence(self, bench: Path) -> None:
        entry = {"swc": "InstrumentCluster", "runnable": "NoSuchRunnable"}
        address = {"layer": "tasks", "element": "Task_100ms", "path": [{"field": "runnables"}, {"member": entry}]}
        answer = handle_request(
            {
                "operation": "change_set",
                "baseline": _baseline(bench),
                "operations": [{"operation": "remove", "address": address}],
            },
            project=bench,
        )
        assert answer["error"] == "broken-reference"
        assert "nearest" not in answer["message"]
        assert answer["message"].endswith("the collection holds no such member")

    def test_a_task_entry_member_step_names_the_nearest_in_the_component(self, bench: Path) -> None:
        path = [{"field": "runnables"}, {"member": "UpdateThermalStatu"}, {"field": "description"}]
        operation = {"operation": "update", "address": {"layer": "swc", "element": "InstrumentCluster", "path": path}}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [{**operation, "value": "x"}]},
            project=bench,
        )
        assert answer["error"] == "broken-reference"
        assert (
            "InstrumentCluster.runnables holds no 'UpdateThermalStatu'; nearest: UpdateThermalStatus."
            in (answer["message"])
        )

    @staticmethod
    def _created_then_updated(project: Path, port: str, address: dict[str, Any]) -> dict[str, Any]:
        """Create ``BrakeControllers`` holding *port*, then update the description at *address*."""
        value = {
            "name": "BrakeControllers",
            "type": "application",
            "group": "application",
            "ports": [{"name": port, "direction": "provide", "interface": "IBrakeRequest"}],
        }
        create = {"operation": "create", "address": {"layer": "swc", "element": "BrakeControllers"}, "value": value}
        update = {"operation": "update", "address": address, "value": "d"}
        return handle_request(
            {"operation": "change_set", "baseline": _baseline(project), "operations": [create, update]},
            project=project,
        )

    def test_an_element_the_change_set_creates_is_not_named_missing(self, bench: Path) -> None:
        """The miss is member ``pY`` of the element op 1 creates, never the element.

        Nothing points the agent at the existing ``BrakeController``."""
        path = [{"field": "ports"}, {"member": "pY"}, {"field": "description"}]
        answer = self._created_then_updated(bench, "pX", {"layer": "swc", "element": "BrakeControllers", "path": path})
        assert answer["error"] == "broken-reference"
        assert "holds no 'BrakeControllers'" not in answer["message"]
        assert "nearest" not in answer["message"]
        assert answer["message"].endswith("path step 1 names a collection member that is not there")

    def test_a_member_the_change_set_creates_is_a_candidate(self, bench: Path) -> None:
        path = [{"field": "ports"}, {"member": "pBrakeStatu"}, {"field": "description"}]
        answer = self._created_then_updated(
            bench, "pBrakeStatus", {"layer": "swc", "element": "BrakeControllers", "path": path}
        )
        assert answer["error"] == "broken-reference"
        assert answer["message"].endswith("BrakeControllers.ports holds no 'pBrakeStatu'; nearest: pBrakeStatus.")

    def test_an_element_the_change_set_creates_is_a_candidate(self, bench: Path) -> None:
        path = [{"field": "ports"}, {"member": "pX"}, {"field": "description"}]
        answer = self._created_then_updated(bench, "pX", {"layer": "swc", "element": "BrakeControlers", "path": path})
        assert answer["error"] == "broken-reference"
        names = answer["message"].rpartition("swc holds no 'BrakeControlers'; nearest: ")[2].rstrip(".").split(", ")
        assert "BrakeControllers" in names

    def test_a_member_created_at_its_own_address_is_a_candidate(self, bench: Path) -> None:
        port = {"name": "pBrakeStatus", "direction": "provide", "interface": "IBrakeRequest"}
        create = {
            "operation": "create",
            "address": _at("swc", "BrakeController", "ports", "pBrakeStatus"),
            "value": port,
        }
        path = [{"field": "ports"}, {"member": "pBrakeStatu"}, {"field": "description"}]
        update = {"operation": "update", "address": {"layer": "swc", "element": "BrakeController", "path": path}}
        answer = handle_request(
            {
                "operation": "change_set",
                "baseline": _baseline(bench),
                "operations": [create, {**update, "value": "d"}],
            },
            project=bench,
        )
        assert answer["error"] == "broken-reference"
        assert answer["message"].endswith("BrakeController.ports holds no 'pBrakeStatu'; nearest: pBrakeStatus.")

    def test_every_broken_reference_group_has_a_pool_or_is_named_exempt(self) -> None:
        groups = {mapping.group or rule for rule, mapping in RULE_CODES.items() if mapping.code == BROKEN_REFERENCE}
        assert groups - set(REFERENCE_POOLS) == set(UNSUGGESTED_GROUPS)
        assert set(REFERENCE_POOLS) <= groups


# remove with dependents


def _at(layer: str, element: str, field: str, member: Any) -> dict[str, Any]:
    return {"layer": layer, "element": element, "path": [{"field": field}, {"member": member}]}


def _rows(answer: dict[str, Any]) -> list[tuple[str, dict[str, Any], str | None]]:
    """The delta in order, each entry as ``(operation, address, origin)``."""
    return [(entry["operation"], entry["address"], entry.get("origin")) for entry in answer["delta"]]


_REMOVE_CLUSTER = {
    "operation": "remove",
    "address": {"layer": "swc", "element": "InstrumentCluster"},
    "with_dependents": True,
}


class TestRemoveWithDependents:
    def test_without_the_flag_the_remove_is_refused(self, bench: Path) -> None:
        operation = {key: value for key, value in _REMOVE_CLUSTER.items() if key != "with_dependents"}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [operation]}, project=bench
        )
        assert answer["error"] == "broken-reference"

    def test_the_delta_names_every_referrer_marked_as_the_servers(self, bench: Path) -> None:
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [_REMOVE_CLUSTER]}, project=bench
        )
        assert answer["status"] == "staged"
        cluster_entry = {"swc": "InstrumentCluster", "instance": None, "runnable": "UpdateThermalStatus"}
        assert _rows(answer) == [
            ("remove", {"layer": "swc", "element": "InstrumentCluster"}, None),
            ("remove", {"layer": "connectors", "element": "ChassisToCluster"}, "server"),
            ("remove", {"layer": "connectors", "element": "DiagnosticsToCluster"}, "server"),
            ("remove", _at("compositions", "VehicleControl", "members", "InstrumentCluster"), "server"),
            ("remove", _at("tasks", "Task_100ms", "runnables", cluster_entry), "server"),
            ("remove", _at("sequences", "thermal_status_display", "steps", cluster_entry), "server"),
        ]

    def test_an_instance_path_and_a_nested_member_are_followed(self, bench: Path) -> None:
        """``BrakeTemperatureSensor`` is typed by two members, one of them in a nested composition.

        The tasks and sequences name it only by ``instance`` paths ending at those members.
        """
        operation = {**_REMOVE_CLUSTER, "address": {"layer": "swc", "element": "BrakeTemperatureSensor"}}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [operation]}, project=bench
        )
        assert answer["status"] == "staged"
        front = {
            "swc": None,
            "instance": "Chassis/BrakeSubsystem/FrontBrakeSensor",
            "runnable": "PublishBrakeTemperature",
        }
        rear = {"swc": None, "instance": "Chassis/RearBrakeSensor", "runnable": "PublishBrakeTemperature"}
        assert _rows(answer) == [
            ("remove", {"layer": "swc", "element": "BrakeTemperatureSensor"}, None),
            ("remove", {"layer": "connectors", "element": "RearSensorToController"}, "server"),
            ("remove", {"layer": "connectors", "element": "SensorToProtection"}, "server"),
            ("remove", _at("compositions", "BrakeSubsystem", "members", "FrontBrakeSensor"), "server"),
            ("remove", _at("compositions", "Chassis", "members", "RearBrakeSensor"), "server"),
            ("remove", _at("tasks", "Task_50ms", "runnables", front), "server"),
            ("remove", _at("tasks", "Task_50ms", "runnables", rear), "server"),
            ("remove", _at("sequences", "brake_thermal_cycle", "steps", front), "server"),
            ("remove", _at("sequences", "brake_thermal_cycle", "steps", rear), "server"),
            ("remove", _at("sequences", "thermal_protection_fast_path", "steps", front), "server"),
            ("remove", _at("sequences", "thermal_protection_fast_path", "steps", rear), "server"),
        ]
        committed = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [operation]},
            project=bench,
            review=_approve_with_safety,
        )
        assert committed["status"] == "committed"

    def test_a_delegated_member_takes_its_delegations_with_it(self, bench: Path) -> None:
        operation = {**_REMOVE_CLUSTER, "address": {"layer": "swc", "element": "BrakeThermalProtection"}}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [operation]},
            project=bench,
            review=_approve_with_safety,
        )
        assert answer["status"] == "committed"
        delegations = [
            row[1]["path"][1]["member"]
            for row in _rows(answer)
            if row[1].get("path", [{}])[0].get("field") == "delegations"
        ]
        assert delegations == [
            "DelegateBrakeRequest",
            "DelegateSubsystemState",
            "DelegatePermittedAction",
            "DelegateSubsystemStatus",
            "DelegateSubsystemEvent",
        ]
        assert all(row[2] == "server" for row in _rows(answer)[1:])

    def test_the_reply_as_sent_summarises_each_element_removed_whole(self, bench: Path) -> None:
        """The response summarises every whole element's ``before``; members go out whole.

        The engine's own answer, which the gates read, is whole throughout.
        """
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [_REMOVE_CLUSTER]}, project=bench
        )
        sent = summarize_response(answer)

        forms = [
            (entry["address"]["layer"], "path" in entry["address"], entry.get("before_form")) for entry in sent["delta"]
        ]
        assert forms == [
            ("swc", False, "summary"),
            ("connectors", False, "summary"),
            ("connectors", False, "summary"),
            ("compositions", True, None),
            ("tasks", True, None),
            ("sequences", True, None),
        ]
        assert isinstance(sent["delta"][0]["before"]["ports"], int)
        assert isinstance(answer["delta"][0]["before"]["ports"], list)
        assert all("before_form" not in entry for entry in answer["delta"])
        assert measure(sent) < measure(answer)

    def test_committed_it_passes_the_benchmarks_task_05(self, bench: Path) -> None:
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [_REMOVE_CLUSTER]},
            project=bench,
            review=_approve,
        )
        assert answer["status"] == "committed"
        check = agent_task_check_module()
        assert check.check_task(bench, check.load_task("05-remove-component"), baseline=AGENT_TASK_BENCH) == []

    def test_a_referrer_the_client_removes_itself_is_not_removed_twice(self, bench: Path) -> None:
        own = {"operation": "remove", "address": {"layer": "connectors", "element": "ChassisToCluster"}}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [own, _REMOVE_CLUSTER]},
            project=bench,
        )
        assert answer["status"] == "staged"
        (row,) = [entry for entry in answer["delta"] if entry["address"]["element"] == "ChassisToCluster"]
        assert "origin" not in row

    def test_a_short_spelled_task_entry_the_client_removes_is_not_removed_twice(self, bench: Path) -> None:
        """The client writes the entry without ``instance``; the server's would carry ``instance: None``."""
        entry = {"swc": "InstrumentCluster", "runnable": "UpdateThermalStatus"}
        own = {"operation": "remove", "address": _at("tasks", "Task_100ms", "runnables", entry)}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [own, _REMOVE_CLUSTER]},
            project=bench,
        )
        assert answer["status"] == "staged"
        tasks = [row for row in _rows(answer) if row[1]["layer"] == "tasks"]
        assert len(tasks) == 1
        assert tasks[0][2] is None

    def test_a_whole_task_the_client_removes_takes_no_server_entry(self, bench: Path) -> None:
        own = {"operation": "remove", "address": {"layer": "tasks", "element": "Task_100ms"}}
        sequence = {"operation": "remove", "address": {"layer": "sequences", "element": "thermal_status_display"}}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [own, sequence, _REMOVE_CLUSTER]},
            project=bench,
        )
        assert answer["status"] == "staged"
        (staged,) = _records(bench, CHANGE_SET_STAGED)
        server = staged["args"]["server_operations"]
        assert {row["address"]["layer"] for row in server} == {"connectors", "compositions"}
        assert [row for row in _rows(answer) if row[1]["layer"] == "tasks"] == [
            ("remove", {"layer": "tasks", "element": "Task_100ms"}, None)
        ]

    def test_two_flagged_removes_sharing_a_connector_remove_it_once(self, bench: Path) -> None:
        manager = {**_REMOVE_CLUSTER, "address": {"layer": "swc", "element": "DiagnosticManager"}}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [manager, _REMOVE_CLUSTER]},
            project=bench,
        )
        assert answer["status"] == "staged"
        addresses = [row[1] for row in _rows(answer)]
        assert addresses.count({"layer": "connectors", "element": "DiagnosticsToCluster"}) == 1
        assert len(addresses) == len({str(address) for address in addresses})
        (staged,) = _records(bench, CHANGE_SET_STAGED)
        server = [str(row["address"]) for row in staged["args"]["server_operations"]]
        assert len(server) == len(set(server))

    def test_two_flagged_removes_emptying_a_shared_sequence_remove_it_whole(self, bench: Path) -> None:
        """``thermal_status_display`` holds exactly the two components' steps: neither alone empties it."""
        manager = {**_REMOVE_CLUSTER, "address": {"layer": "swc", "element": "DiagnosticManager"}}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [manager, _REMOVE_CLUSTER]},
            project=bench,
            review=_approve_with_safety,
        )
        assert answer["status"] == "committed"
        sequences = [row for row in _rows(answer) if row[1]["layer"] == "sequences"]
        assert sequences == [("remove", {"layer": "sequences", "element": "thermal_status_display"}, "server")]

    def test_a_client_step_removal_and_a_flagged_remove_emptying_a_sequence_remove_it_whole(self, bench: Path) -> None:
        step = {"swc": "DiagnosticManager", "runnable": "ProcessDiagnosticEvent"}
        own = {"operation": "remove", "address": _at("sequences", "thermal_status_display", "steps", step)}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [own, _REMOVE_CLUSTER]},
            project=bench,
            review=_approve,
        )
        assert answer["status"] == "committed"
        sequences = [row for row in _rows(answer) if row[1]["layer"] == "sequences"]
        assert sequences == [("remove", {"layer": "sequences", "element": "thermal_status_display"}, "server")]

    def test_a_step_the_client_creates_keeps_the_sequence_the_flagged_removes_empty(self, bench: Path) -> None:
        """A step the client creates keeps the sequence the flagged removes would empty.

        The two flagged removes take both of ``thermal_status_display``'s steps and the client's create gives it a
        third, so the sequence stays with the client's step and the server removes the two."""
        step = {"swc": "VehicleStateManager", "runnable": "PublishOperatingState"}
        create = {"operation": "create", "address": _at("sequences", "thermal_status_display", "steps", step)}
        manager = {**_REMOVE_CLUSTER, "address": {"layer": "swc", "element": "DiagnosticManager"}}
        answer = handle_request(
            {
                "operation": "change_set",
                "baseline": _baseline(bench),
                "operations": [{**create, "value": step}, manager, _REMOVE_CLUSTER],
            },
            project=bench,
            review=_approve_with_safety,
        )
        assert answer["status"] == "committed"
        full = {**step, "instance": None}
        sequences = [row for row in _rows(answer) if row[1]["layer"] == "sequences"]
        assert sorted(sequences, key=str) == sorted(
            [
                ("create", _at("sequences", "thermal_status_display", "steps", full), None),
                (
                    "remove",
                    _at(
                        "sequences",
                        "thermal_status_display",
                        "steps",
                        {"swc": "DiagnosticManager", "instance": None, "runnable": "ProcessDiagnosticEvent"},
                    ),
                    "server",
                ),
                (
                    "remove",
                    _at(
                        "sequences",
                        "thermal_status_display",
                        "steps",
                        {"swc": "InstrumentCluster", "instance": None, "runnable": "UpdateThermalStatus"},
                    ),
                    "server",
                ),
            ],
            key=str,
        )
        state = load_model_state(bench / "model")
        (sequence,) = [held for held in state.layers["sequences"] if held.name == "thermal_status_display"]
        assert [(held.swc, held.runnable) for held in sequence.steps] == [
            ("VehicleStateManager", "PublishOperatingState")
        ]

    @staticmethod
    def _replacement(bench: Path, old: str, new: str) -> dict[str, Any]:
        """A ``create`` of *new*, a copy of component *old*."""
        value = dict(
            handle_request({"operation": "read", "address": {"layer": "swc", "element": old}}, project=bench)["value"]
        )
        return {"operation": "create", "address": {"layer": "swc", "element": new}, "value": {**value, "name": new}}

    def test_a_referrer_the_clients_update_moves_off_the_component_is_left_as_the_client_made_it(
        self, bench: Path
    ) -> None:
        """The replace workflow: a referrer the client's update moves off the component stays as the client made it.

        The client re-points ``ChassisToCluster`` and retypes the ``VehicleControl`` member at a copy of the component;
        the server unlinks only what still names it after the client's operations, and the client's two updates are the
        delta's rows."""
        member = _at("compositions", "VehicleControl", "members", "InstrumentCluster")
        operations = [
            self._replacement(bench, "InstrumentCluster", "InstrumentCluster2"),
            {
                "operation": "update",
                "address": {**member, "path": [*member["path"], {"field": "type"}]},
                "value": "InstrumentCluster2",
            },
            {
                "operation": "update",
                "address": {"layer": "connectors", "element": "ChassisToCluster", "path": [{"field": "to"}]},
                "value": {"swc": "InstrumentCluster2", "port": "rThermalProtectionStatus"},
            },
            _REMOVE_CLUSTER,
        ]
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": operations},
            project=bench,
            review=_approve_with_safety,
        )
        assert answer["status"] == "committed"
        rows = _rows(answer)
        assert ("remove", {"layer": "connectors", "element": "DiagnosticsToCluster"}, "server") in rows
        assert [row for row in rows if row[1]["element"] in ("ChassisToCluster", "VehicleControl")] == [
            (
                "update",
                {"layer": "connectors", "element": "ChassisToCluster", "path": [{"field": "to"}, {"field": "swc"}]},
                None,
            ),
            ("update", {**member, "path": [*member["path"], {"field": "type"}]}, None),
        ]
        state = load_model_state(bench / "model")
        (connector,) = [held for held in state.layers["connectors"] if held.name == "ChassisToCluster"]
        assert connector.to.swc == "InstrumentCluster2"
        (composition,) = [held for held in state.layers["compositions"] if held.name == "VehicleControl"]
        assert [held.type for held in composition.members if held.name == "InstrumentCluster"] == ["InstrumentCluster2"]

    def test_a_retyped_member_keeps_the_entries_its_instance_path_reaches(self, bench: Path) -> None:
        """``RearBrakeSensor`` is retyped at a copy of ``BrakeTemperatureSensor``.

        The task entry and the sequence steps whose ``instance`` path ends at it stay, and only the front
        sensor's go.
        """
        rear = {"swc": None, "instance": "Chassis/RearBrakeSensor", "runnable": "PublishBrakeTemperature"}
        front = {**rear, "instance": "Chassis/BrakeSubsystem/FrontBrakeSensor"}
        member = _at("compositions", "Chassis", "members", "RearBrakeSensor")
        operations = [
            self._replacement(bench, "BrakeTemperatureSensor", "BrakeTemperatureSensor2"),
            {
                "operation": "update",
                "address": {**member, "path": [*member["path"], {"field": "type"}]},
                "value": "BrakeTemperatureSensor2",
            },
            {
                "operation": "update",
                "address": {
                    "layer": "connectors",
                    "element": "RearSensorToController",
                    "path": [{"field": "from"}, {"field": "swc"}],
                },
                "value": "BrakeTemperatureSensor2",
            },
            {**_REMOVE_CLUSTER, "address": {"layer": "swc", "element": "BrakeTemperatureSensor"}},
        ]
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": operations},
            project=bench,
            review=_approve_with_safety,
        )
        assert answer["status"] == "committed"
        server = [row[1] for row in _rows(answer) if row[2] == "server"]
        assert server == [
            {"layer": "connectors", "element": "SensorToProtection"},
            _at("compositions", "BrakeSubsystem", "members", "FrontBrakeSensor"),
            _at("tasks", "Task_50ms", "runnables", front),
            _at("sequences", "brake_thermal_cycle", "steps", front),
            _at("sequences", "thermal_protection_fast_path", "steps", front),
        ]
        state = load_model_state(bench / "model")
        (task,) = [held for held in state.layers["tasks"] if held.name == "Task_50ms"]
        assert "Chassis/RearBrakeSensor" in [entry.instance for entry in task.runnables]

    def test_a_retyped_member_keeps_its_delegations(self, bench: Path) -> None:
        member = _at("compositions", "BrakeSubsystem", "members", "BrakeThermalProtection")
        operations = [
            self._replacement(bench, "BrakeThermalProtection", "BrakeThermalProtection2"),
            {
                "operation": "update",
                "address": {**member, "path": [*member["path"], {"field": "type"}]},
                "value": "BrakeThermalProtection2",
            },
            {**_REMOVE_CLUSTER, "address": {"layer": "swc", "element": "BrakeThermalProtection"}},
        ]
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": operations}, project=bench
        )
        assert answer["status"] == "staged"
        assert [row for row in _rows(answer) if row[1]["layer"] == "compositions"] == [
            ("update", {**member, "path": [*member["path"], {"field": "type"}]}, None)
        ]

    @pytest.mark.parametrize(
        ("composition", "member"),
        [
            pytest.param("Chassis", "RearBrakeSensor", id="rear"),
            pytest.param("BrakeSubsystem", "FrontBrakeSensor", id="front-nested"),
        ],
    )
    def test_a_member_the_client_removes_still_takes_the_entries_its_instance_path_reached(
        self, bench: Path, composition: str, member: str
    ) -> None:
        """A member removed is not a member retyped.

        The client removes the member itself; the task entry and the sequence steps whose ``instance`` path ended at it
        are still the server's to unlink."""
        rear = {"swc": None, "instance": "Chassis/RearBrakeSensor", "runnable": "PublishBrakeTemperature"}
        front = {**rear, "instance": "Chassis/BrakeSubsystem/FrontBrakeSensor"}
        own = {"operation": "remove", "address": _at("compositions", composition, "members", member)}
        flagged = {**_REMOVE_CLUSTER, "address": {"layer": "swc", "element": "BrakeTemperatureSensor"}}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [own, flagged]},
            project=bench,
            review=_approve_with_safety,
        )
        assert answer["status"] == "committed"
        rows = _rows(answer)
        assert ("remove", own["address"], None) in rows
        for entry in (front, rear):
            assert ("remove", _at("tasks", "Task_50ms", "runnables", entry), "server") in rows
            for sequence in ("brake_thermal_cycle", "thermal_protection_fast_path"):
                assert ("remove", _at("sequences", sequence, "steps", entry), "server") in rows
        state = load_model_state(bench / "model")
        (task,) = [held for held in state.layers["tasks"] if held.name == "Task_50ms"]
        assert not {front["instance"], rear["instance"]} & {entry.instance for entry in task.runnables}

    def test_a_member_the_client_removes_still_takes_its_delegations(self, bench: Path) -> None:
        own = {
            "operation": "remove",
            "address": _at("compositions", "BrakeSubsystem", "members", "BrakeThermalProtection"),
        }
        flagged = {**_REMOVE_CLUSTER, "address": {"layer": "swc", "element": "BrakeThermalProtection"}}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [own, flagged]},
            project=bench,
            review=_approve_with_safety,
        )
        assert answer["status"] == "committed"
        delegations = [
            (row[1]["path"][1]["member"], row[2])
            for row in _rows(answer)
            if row[1].get("path", [{}])[0].get("field") == "delegations"
        ]
        assert delegations == [
            ("DelegateBrakeRequest", "server"),
            ("DelegateSubsystemState", "server"),
            ("DelegatePermittedAction", "server"),
            ("DelegateSubsystemStatus", "server"),
            ("DelegateSubsystemEvent", "server"),
        ]

    def test_a_composition_the_client_removes_still_takes_the_entries_a_path_through_it_reached(
        self, bench: Path
    ) -> None:
        """The client removes ``BrakeSubsystem`` whole, with what names it.

        The ``instance`` path ``Chassis/BrakeSubsystem/FrontBrakeSensor`` walks through it, and its entry and
        steps are the server's.
        """
        front = {
            "swc": None,
            "instance": "Chassis/BrakeSubsystem/FrontBrakeSensor",
            "runnable": "PublishBrakeTemperature",
        }
        own = [
            {"operation": "remove", "address": {"layer": "compositions", "element": "BrakeSubsystem"}},
            {"operation": "remove", "address": _at("compositions", "Chassis", "members", "BrakeSubsystem")},
            *(
                {"operation": "remove", "address": _at("compositions", "Chassis", "delegations", name)}
                for name in ("DelegateOperatingState", "DelegateThermalStatus", "DelegateThermalEvent")
            ),
            *(
                {"operation": "remove", "address": {"layer": "connectors", "element": name}}
                for name in ("ControllerToSubsystem", "SubsystemToController")
            ),
        ]
        flagged = [
            {**_REMOVE_CLUSTER, "address": {"layer": "swc", "element": name}}
            for name in ("BrakeTemperatureSensor", "BrakeThermalProtection")
        ]
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [*own, *flagged]},
            project=bench,
            review=_approve_with_safety,
        )
        assert answer["status"] == "committed"
        rows = _rows(answer)
        assert ("remove", _at("tasks", "Task_50ms", "runnables", front), "server") in rows
        assert ("remove", _at("sequences", "brake_thermal_cycle", "steps", front), "server") in rows
        assert not [row for row in rows if row[1]["element"] == "BrakeSubsystem" and row[2] == "server"]

    def test_an_unparseable_model_file_is_a_consistency_violation(self, bench: Path) -> None:
        (bench / "model" / "hardware" / "Broken.yaml").write_text("name: [\n", encoding="utf-8", newline="\n")
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [_REMOVE_CLUSTER]}, project=bench
        )
        assert answer["error"] == "consistency-violation"
        assert "'hardware' element 'Broken'" in answer["message"]
        assert ".yaml" not in answer["message"]
        assert str(bench) not in answer["message"]
        assert len(_records(bench, "change_set_rejected")) == 1

    @pytest.mark.parametrize("flag", ["yes", 1, None], ids=["string", "integer", "null"])
    def test_a_flag_that_is_not_a_boolean_is_a_schema_violation(self, bench: Path, flag: Any) -> None:
        operation = {**_REMOVE_CLUSTER, "with_dependents": flag}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [operation]}, project=bench
        )
        assert answer["error"] == "schema-violation"

    def test_a_sequence_losing_its_last_step_goes_whole(self, bench: Path) -> None:
        sequence = bench / "model" / "sequences" / "thermal_status_display.yaml"
        text = sequence.read_text(encoding="utf-8")
        head, _, _ = text.partition("- swc: DiagnosticManager")
        tail = text[text.index("- swc: InstrumentCluster") :]
        sequence.write_text(head + tail, encoding="utf-8", newline="\n")
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [_REMOVE_CLUSTER]}, project=bench
        )
        (row,) = [entry for entry in answer["delta"] if entry["address"]["layer"] == "sequences"]
        assert row["address"] == {"layer": "sequences", "element": "thermal_status_display"}
        assert row["origin"] == "server"

    @pytest.mark.parametrize(
        "address",
        [
            {"layer": "interfaces", "element": "IOdometer"},
            {"layer": "swc", "element": "InstrumentCluster", "path": [{"field": "ports"}, {"member": "rOdometer"}]},
        ],
        ids=["another-layer", "a-member"],
    )
    def test_the_flag_elsewhere_is_a_schema_violation(self, bench: Path, address: dict) -> None:
        operation = {"operation": "remove", "address": address, "with_dependents": True}
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [operation]}, project=bench
        )
        assert answer["error"] == "schema-violation"


# count


class TestCount:
    def test_a_filtered_layer(self, bench: Path) -> None:
        request = {
            "operation": "read",
            "address": {"layer": "swc"},
            "form": "count",
            "where": [{"field": "asil", "equals": "B"}],
        }
        answer = handle_request(request, project=bench)
        assert answer == {
            "response": "count",
            "address": {"layer": "swc"},
            "count": 1,
            "where": [{"field": "asil", "equals": "B"}],
        }

    def test_a_member_slice_and_a_collection_agree(self, bench: Path) -> None:
        sliced = handle_request(
            {
                "operation": "read",
                "address": {"layer": "swc"},
                "form": "count",
                "members": "ports",
                "where": [{"field": "interface", "equals": "IOdometer"}],
            },
            project=bench,
        )
        listed = handle_request(
            {
                "operation": "read",
                "address": {"layer": "swc"},
                "form": "index",
                "members": "ports",
                "where": [{"field": "interface", "equals": "IOdometer"}],
            },
            project=bench,
        )
        assert sliced["count"] == listed["value"]["count"] == 1
        collection = handle_request(
            {
                "operation": "read",
                "address": {"layer": "swc", "element": "InstrumentCluster", "path": [{"field": "ports"}]},
                "form": "count",
            },
            project=bench,
        )
        assert collection["count"] == 3

    def test_a_row_too_wide_to_publish_is_still_counted(self, bench: Path) -> None:
        """The count's page of one is discarded, so it is never measured."""
        target = bench / "model" / "swc" / "application" / "DiagnosticManager.yaml"
        sleep = "      mode_ref: /Platform/ModeDeclarationGroups/EcuModes/SLEEP\n"
        modes = "".join(
            "    - port: rEcuMode\n"
            "      mode_group_ref: /Platform/Interfaces/EcuMode/currentMode\n"
            f"      mode_ref: /Platform/ModeDeclarationGroups/EcuModes/M{index:04d}\n"
            for index in range(800)
        )
        text = target.read_text(encoding="utf-8")
        assert text.count(sleep) == 1
        target.write_text(text.replace(sleep, sleep + modes), encoding="utf-8", newline="\n")
        path = [{"field": "runnables"}, {"member": "ProcessDiagnosticEvent"}, {"field": "events"}]
        address = {"layer": "swc", "element": "DiagnosticManager", "path": path}
        listed = handle_request({"operation": "read", "address": address, "form": "index"}, project=bench)
        assert listed["error"] == "over-budget"
        counted = handle_request({"operation": "read", "address": address, "form": "count"}, project=bench)
        assert counted == {"response": "count", "address": address, "count": 1}

    def test_a_derived_read_counts_its_rows(self, bench: Path) -> None:
        request = {"operation": "read", "address": {"layer": "tasks"}, "derived": ["core"]}
        count = handle_request({**request, "form": "count"}, project=bench)
        rows = handle_request({**request, "form": "index"}, project=bench)
        assert count["count"] == rows["value"]["count"] == 3

    @pytest.mark.parametrize("page", [{"limit": 2}, {"after": "BrakeController"}], ids=["limit", "after"])
    def test_a_page_is_refused_with_a_count(self, bench: Path, page: dict[str, Any]) -> None:
        request = {"operation": "read", "address": {"layer": "swc"}, "form": "count", **page}
        answer = handle_request(request, project=bench)
        assert answer["error"] == "schema-violation"
        assert "a count is one number" in answer["message"]

    @pytest.mark.parametrize(
        ("address", "names"),
        [
            ({"layer": "swc", "element": "BrakeController"}, "an element"),
            ({"layer": "swc", "element": "BrakeController", "path": [{"field": "asil"}]}, "a single value"),
        ],
        ids=["element", "single-value"],
    )
    def test_a_count_at_an_address_naming_no_set_is_refused_for_what_it_counts(
        self, bench: Path, address: dict[str, Any], names: str
    ) -> None:
        answer = handle_request({"operation": "read", "address": address, "form": "count"}, project=bench)
        assert answer["error"] == "schema-violation"
        assert answer["message"] == (
            "a count counts a set, so it applies at a layer address or at a collection address; "
            f"this address names {names}"
        )
        assert "limit" not in answer["message"]

    def test_a_count_takes_one_address(self, bench: Path) -> None:
        request = {"operation": "read", "addresses": [{"layer": "swc"}], "form": "count"}
        answer = handle_request(request, project=bench)
        assert answer["error"] == "schema-violation"
        assert "one `address`" in answer["message"]

    def test_an_answer_over_the_response_budget_is_refused(self, bench: Path) -> None:
        """The count's echo of its ``where`` is measured, not only its page."""
        request = {
            "operation": "read",
            "address": {"layer": "swc"},
            "form": "count",
            "where": [{"field": "description", "equals": "x" * 90_000}],
        }
        answer = handle_request(request, project=bench)
        assert answer["error"] == "over-budget"
        assert answer["addresses"] == [{"layer": "swc"}]
        assert answer["measured"]["wire_bytes"] > RESPONSE_BUDGET_WIRE_BYTES
        assert answer["measured"]["elements"] == 1
        assert "hints" not in answer
        assert measure(answer) < 1_000

    @pytest.mark.parametrize("form", [None, "index", "full", "summary"], ids=lambda form: f"form-{form}")
    def test_the_parser_refuses_a_request_that_does_not_state_form_count(self, form: str | None) -> None:
        """``$defs/countRequest`` requires ``form: "count"``; the parser checks it rather than writing it."""
        request = {"operation": "read", "address": {"layer": "swc"}} | ({} if form is None else {"form": form})
        with pytest.raises(EnvelopeError, match='form: "count"'):
            CountRequest.from_json(request)


class TestAWhereOverADerivedValueOnTheProtocol:
    """``where`` over ``derived.NAME.PATH``: the kernel filters the computed rows before the page and the count."""

    RATED_B = {
        "operation": "read",
        "address": {"layer": "swc"},
        "form": "index",
        "derived": ["effective_level"],
        "where": [{"field": "derived.effective_level.rating", "equals": "B"}],
    }

    def test_the_rating_clause_answers_exactly_the_three(self, bench: Path) -> None:
        answer = handle_request(self.RATED_B, project=bench)
        assert [row["element"] for row in answer["value"]["identities"]] == [
            "BrakeController",
            "BrakeTemperatureSensor",
            "BrakeThermalProtection",
        ]
        assert {row["derived"]["effective_level"]["level"] for row in answer["value"]["identities"]} == {"B", "B(D)"}

    def test_count_with_the_rating_clause_answers_3_and_nothing_else(self, bench: Path) -> None:
        answer = handle_request({**self.RATED_B, "form": "count"}, project=bench)
        assert answer == {
            "response": "count",
            "address": {"layer": "swc"},
            "count": 3,
            "where": self.RATED_B["where"],
            "derived": ["effective_level"],
        }

    def test_exists_on_the_core_answers_the_tasks_whose_partition_names_one(self, bench: Path) -> None:
        """A partition whose ``core`` resolves to no declared core computes ``core: null`` -- no value."""
        partition = bench / "model" / "hardware" / "P_Vehicle.yaml"
        text = partition.read_text(encoding="utf-8")
        assert "core: Core0\n" in text
        partition.write_text(text.replace("core: Core0\n", "core: NoSuchCore\n"), encoding="utf-8", newline="\n")
        request = {"operation": "read", "address": {"layer": "tasks"}, "form": "index", "derived": ["core"]}
        every = handle_request(request, project=bench)["value"]["identities"]
        assert {row["element"]: row["derived"]["core"]["core"] for row in every} == {
            "Task_100ms": None,
            "Task_100ms_Low": None,
            "Task_50ms": "Core1",
        }
        for exists, expected in ((True, ["Task_50ms"]), (False, ["Task_100ms", "Task_100ms_Low"])):
            where = [{"field": "derived.core.core", "exists": exists}]
            answer = handle_request({**request, "where": where}, project=bench)
            assert [row["element"] for row in answer["value"]["identities"]] == expected

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            pytest.param(
                {"derived": ["partitions"]},
                "`where` 'derived.effective_level.rating': a where over a derived value asks for it in `derived`, "
                "and this request's `derived` names ['partitions']",
                id="a-derived-not-asked-for",
            ),
            pytest.param(
                {"form": "summary", "derived": None},
                "`where` 'derived.effective_level.rating': a where over a derived value asks for it in `derived`, "
                "and this request's `derived` is absent",
                id="no-derived-at-all",
            ),
            pytest.param(
                {"where": [{"field": "derived.effective_level.grade", "equals": "B"}]},
                "`where` 'derived.effective_level.grade' names no value of the row: a where over a derived value "
                "names one value of a computed value the request asks for, and this row's are "
                "['derived.effective_level.level', 'derived.effective_level.rating']",
                id="a-path-the-shape-lacks",
            ),
        ],
    )
    def test_the_two_refusals_are_unknown_field(self, bench: Path, change: dict[str, Any], message: str) -> None:
        request = {key: value for key, value in {**self.RATED_B, **change}.items() if value is not None}
        for form in (request.get("form"), "count"):
            refused = handle_request({**request, "form": form}, project=bench)
            assert refused["error"] == "unknown-field", refused
            assert refused["message"] == message
            assert refused["addresses"] == [{"layer": "swc"}]

    def test_a_declared_clause_is_still_refused_first(self, bench: Path) -> None:
        request = {**self.RATED_B, "where": [*self.RATED_B["where"], {"field": "colour", "equals": "red"}]}
        refused = handle_request(request, project=bench)
        assert refused["error"] == "unknown-field"
        assert refused["message"].startswith("'colour' is not a declared field")


# The server's rows: scope gates, audit, complete, origin


def _records(project: Path, operation: str) -> list[dict[str, Any]]:
    return [
        entry
        for entry in AuditLog(str(project / "dist" / AUDIT_FILENAME)).get_entries()
        if entry["operation"] == operation
    ]


class TestTheServersRows:
    def test_a_client_operation_outside_the_scope_is_refused_before_the_model_is_read(
        self, bench: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty scope over a malformed model is ``unauthorized-delta``, not the model's refusal."""
        baseline = _baseline(bench)
        (bench / "model" / "hardware" / "Broken.yaml").write_text("name: [\n", encoding="utf-8", newline="\n")

        def load(model_root: str) -> Any:
            raise AssertionError("the model was read before gate 1 refused the client's own operation")

        monkeypatch.setattr(engine, "load_snapshot", load)
        answer = handle_request(
            {"operation": "change_set", "baseline": baseline, "operations": [_REMOVE_CLUSTER]},
            project=bench,
            approved_scope=[],
        )
        assert answer["error"] == "unauthorized-delta"

    def test_a_server_row_outside_the_approved_scope_is_refused(self, bench: Path) -> None:
        scope = [{"operation": "remove", "address": {"layer": "swc", "element": "InstrumentCluster"}}]
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [_REMOVE_CLUSTER]},
            project=bench,
            approved_scope=scope,
        )
        assert answer["error"] == "unauthorized-delta"
        assert _records(bench, CHANGE_SET_STAGED) == []

    def test_a_scope_covering_every_referrer_admits_the_set(self, bench: Path) -> None:
        scope = [
            {"operation": "remove", "address": {"layer": layer, "element": element}}
            for layer, element in (
                ("swc", "InstrumentCluster"),
                ("connectors", "ChassisToCluster"),
                ("connectors", "DiagnosticsToCluster"),
                ("sequences", "thermal_status_display"),
            )
        ] + [
            {"operation": operation, "address": {"layer": layer, "element": element}}
            for operation in ("update", "remove")
            for layer, element in (("compositions", "VehicleControl"), ("tasks", "Task_100ms"))
        ]
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [_REMOVE_CLUSTER]},
            project=bench,
            approved_scope=scope,
        )
        assert answer["status"] == "staged"

    def test_the_audit_records_the_server_rows_apart(self, bench: Path) -> None:
        answer = handle_request(
            {"operation": "change_set", "baseline": _baseline(bench), "operations": [_REMOVE_CLUSTER]}, project=bench
        )
        assert answer["status"] == "staged"
        (staged,) = _records(bench, CHANGE_SET_STAGED)
        assert staged["args"]["operations"] == [
            {"operation": "remove", "address": {"layer": "swc", "element": "InstrumentCluster"}}
        ]
        server = staged["args"]["server_operations"]
        assert {"operation": "remove", "address": {"layer": "connectors", "element": "ChassisToCluster"}} in server
        assert {row["address"]["layer"] for row in server} == {"connectors", "compositions", "tasks", "sequences"}
        server_rows = [entry for entry in answer["delta"] if entry.get("origin") == "server"]
        assert len(server_rows) == len(server) == 5

    def test_with_dependents_beside_complete_true(self, bench: Path) -> None:
        """Nothing to repair: the dependents are expanded, pass D accepts, ``complete`` adds nothing."""
        answer = handle_request(
            {
                "operation": "change_set",
                "baseline": _baseline(bench),
                "complete": True,
                "operations": [_REMOVE_CLUSTER],
            },
            project=bench,
        )
        assert answer["status"] == "staged"
        (staged,) = _records(bench, CHANGE_SET_STAGED)
        assert staged["args"]["complete"] is True
        assert all(row["operation"] == "remove" for row in staged["args"]["server_operations"])


class TestOrigin:
    """``origin``: a containing entry is both parties' only when a client operation touches that element."""

    @staticmethod
    def _entry(element: str, *path: Any) -> DeltaEntry:
        return DeltaEntry(operation="update", address=Address(layer="tasks", element=element, path=path), after=[])

    _SERVER = (
        RemoveOperation(
            address=Address(
                layer="tasks",
                element="Task_100ms",
                path=(FieldStep("runnables"), MemberStep({"swc": "X", "instance": None, "runnable": "r"})),
            )
        ),
    )

    def test_the_servers_own_address_is_server(self) -> None:
        (marked,) = _mark_server_entries(
            [
                DeltaEntry(
                    operation="remove",
                    address=self._SERVER[0].address,
                )
            ],
            self._SERVER,
            reg.current(),
            (),
        )
        assert marked.origin == "server"

    def test_a_containing_entry_no_client_operation_touches_is_server(self) -> None:
        (marked,) = _mark_server_entries(
            [self._entry("Task_100ms", FieldStep("runnables"))], self._SERVER, reg.current(), ()
        )
        assert marked.origin == "server"

    def test_a_containing_entry_a_client_operation_touches_is_both_parties(self) -> None:
        client = (
            UpdateOperation(
                address=Address(layer="tasks", element="Task_100ms", path=(FieldStep("priority"),)), value=3
            ),
        )
        (marked,) = _mark_server_entries(
            [self._entry("Task_100ms", FieldStep("runnables"))], self._SERVER, reg.current(), client
        )
        assert marked.origin == "client_and_server"

    def test_an_entry_no_server_operation_reaches_carries_no_origin(self) -> None:
        (marked,) = _mark_server_entries(
            [self._entry("Task_10ms", FieldStep("runnables"))], self._SERVER, reg.current(), ()
        )
        assert marked.origin is None

    def test_a_server_create_inside_a_client_create_stays_both_parties(self) -> None:
        server = (
            CreateOperation(
                address=Address(
                    layer="swc", element="Logger", path=(FieldStep("data_type_mappings"), MemberStep("Thermal"))
                ),
                value="Thermal",
            ),
        )
        client = (CreateOperation(address=Address(layer="swc", element="Logger"), value={}),)
        (marked,) = _mark_server_entries(
            [DeltaEntry(operation="create", address=Address(layer="swc", element="Logger"), after={})],
            server,
            reg.current(),
            client,
        )
        assert marked.origin == "client_and_server"


# confirm_links


def _version(requirement: str, value: Any, **flags: Any) -> dict[str, Any]:
    return {
        "operation": "update",
        "address": {"layer": "requirements", "element": requirement, "path": [{"field": "version"}]},
        "value": value,
        **flags,
    }


def _confirm(requirement: str, value: Any) -> dict[str, Any]:
    return _version(requirement, value, confirm_links=True)


def _link(layer: str, element: str, member: str, *owner: Any) -> dict[str, Any]:
    """The address of the ``satisfies`` member *member*, under *owner*'s ``(field, member)`` steps when given."""
    path = [{"field": owner[0]}, {"member": owner[1]}] if owner else []
    return {"layer": layer, "element": element, "path": [*path, {"field": "satisfies"}, {"member": member}]}


def _change_set(project: Path, *operations: dict[str, Any], **request: Any) -> dict[str, Any]:
    return {"operation": "change_set", "baseline": _baseline(project), "operations": list(operations), **request}


#: ``REQ_BTP_CORE_LOAD``, linked at ``@1`` on the bench; the plant adds the seven site kinds and a requirement
#: deriving from it. The interface's ``@2`` beside its ``@1`` is a ``trace.duplicate-link`` a change set may
#: not create, so it is planted by hand after the change set (:data:`_HAND_PLANTED`).
_PLANTED = (
    ("swc", "InstrumentCluster", "REQ_BTP_CORE_LOAD@1", ()),
    ("swc", "InstrumentCluster", "REQ_BTP_CORE_LOAD", ("ports", "rOdometer")),
    ("swc", "InstrumentCluster", "REQ_BTP_CORE_LOAD@1", ("runnables", "UpdateThermalStatus")),
    ("interfaces", "IOdometer", "REQ_BTP_CORE_LOAD@1", ()),
    ("compositions", "VehicleControl", "REQ_BTP_CORE_LOAD@1", ()),
)

#: The member the plant writes by hand: ``IOdometer``'s second link to the same requirement.
_HAND_PLANTED = ("IOdometer", "REQ_BTP_CORE_LOAD@2")


@pytest.fixture
def planted(bench: Path) -> Path:
    operations = [
        {"operation": "create", "address": _link(layer, element, member, *owner), "value": member}
        for layer, element, member, owner in _PLANTED
    ]
    derived = {
        "layer": "requirements",
        "element": "REQ_BTP_SAFETY_GOAL",
        "path": [{"field": "derived_from"}, {"member": "REQ_BTP_CORE_LOAD@1"}],
    }
    operations.append({"operation": "create", "address": derived, "value": "REQ_BTP_CORE_LOAD@1"})
    answer = handle_request(_change_set(bench, *operations), project=bench, review=_approve_with_safety)
    assert answer["status"] == "committed", answer
    interface, member = _HAND_PLANTED
    [path] = [path for path in (bench / "model" / "interfaces").rglob(f"{interface}.yaml")]
    document = load_yaml_document(path.read_text(encoding="utf-8"))
    document["satisfies"].append(member)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return bench


def _server_rows(answer: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(entry["operation"], entry["address"]) for entry in answer["delta"] if entry.get("origin") == "server"]


class TestConfirmLinks:
    def test_the_showcases_stale_case_is_confirmed_at_its_current_version(self, bench: Path) -> None:
        """``REQ_BTP_TEMPERATURE_RANGE`` is at version 2 and its three interface links at ``@1``.

        The re-import case, an update to the current value -- the field delta is empty and the server's rows are
        the whole delta.
        """
        answer = handle_request(_change_set(bench, _confirm("REQ_BTP_TEMPERATURE_RANGE", 2)), project=bench)
        assert answer["status"] == "staged", answer
        expected = []
        for interface in ("IBrakeDiagnostics", "IBrakeTemperature", "IThermalProtectionStatus"):
            expected.append(("remove", _link("interfaces", interface, "REQ_BTP_TEMPERATURE_RANGE@1")))
            expected.append(("create", _link("interfaces", interface, "REQ_BTP_TEMPERATURE_RANGE@2")))
        assert _rows(answer) == [(operation, address, "server") for operation, address in expected]

    def test_committed_it_clears_the_two_version_warnings(self, bench: Path) -> None:
        from swcraft_tools.validator import check_cross_layer_consistency

        def confirming() -> list[Any]:
            return [
                record
                for record in check_cross_layer_consistency(str(bench / "model")).warnings
                if record.rule in {"trace.link-needs-confirmation", "trace.link-version-not-stated"}
                and record.target == "REQ_BTP_TEMPERATURE_RANGE"
            ]

        assert len(confirming()) == 3
        answer = handle_request(
            _change_set(bench, _confirm("REQ_BTP_TEMPERATURE_RANGE", 2)), project=bench, review=_approve
        )
        assert answer["status"] == "committed"
        assert confirming() == []

    def test_committed_it_passes_the_benchmarks_task_24(self, bench: Path) -> None:
        """The checker's nothing-else-moved rule over the committed set: the version and the six links, nothing else."""
        answer = handle_request(_change_set(bench, _confirm("REQ_BTP_CORE_LOAD", 2)), project=bench, review=_approve)
        assert answer["status"] == "committed"
        check = agent_task_check_module()
        task = check.load_task("24-confirm-links-at-a-new-version")
        assert check.check_task(bench, task, baseline=AGENT_TASK_BENCH) == []

    def test_every_site_kind_is_rewritten_as_a_remove_and_a_create(self, planted: Path) -> None:
        answer = handle_request(_change_set(planted, _confirm("REQ_BTP_CORE_LOAD", 2)), project=planted)
        assert answer["status"] == "staged", answer
        rows = _server_rows(answer)
        old, new = "REQ_BTP_CORE_LOAD@1", "REQ_BTP_CORE_LOAD@2"
        pairs = [
            _link("swc", "InstrumentCluster", old),
            _link("swc", "InstrumentCluster", old, "runnables", "UpdateThermalStatus"),
            _link("compositions", "VehicleControl", old),
            *(_link("tasks", task, old) for task in ("Task_100ms", "Task_100ms_Low", "Task_50ms")),
            *(_link("hardware", element, old) for element in ("CPU", "Core0", "Core1")),
        ]
        for address in pairs:
            created = {**address, "path": [*address["path"][:-1], {"member": new}]}
            assert ("remove", address) in rows and ("create", created) in rows, address
        # The port's bare link is rewritten like a stated one.
        assert ("remove", _link("swc", "InstrumentCluster", "REQ_BTP_CORE_LOAD", "ports", "rOdometer")) in rows
        assert ("create", _link("swc", "InstrumentCluster", new, "ports", "rOdometer")) in rows
        # The interface already holds the new version: the remove alone.
        assert ("remove", _link("interfaces", "IOdometer", old)) in rows
        assert not any(address["element"] == "IOdometer" and operation == "create" for operation, address in rows)
        assert len(rows) == 2 * len(pairs) + 2 + 1
        # A requirement's `derived_from` member is no link.
        assert all(address["layer"] != "requirements" for _, address in rows)

    def test_committed_the_links_are_at_the_new_version_and_derived_from_is_untouched(self, planted: Path) -> None:
        answer = handle_request(
            _change_set(planted, _confirm("REQ_BTP_CORE_LOAD", 2)), project=planted, review=_approve_with_safety
        )
        assert answer["status"] == "committed", answer
        state = load_model_state(planted / "model")
        from swcraft_tools.trace import link_sites_of_state

        values = [site.value for _, _, site in link_sites_of_state(state.layers) if "REQ_BTP_CORE_LOAD" in site.value]
        assert values and set(values) == {"REQ_BTP_CORE_LOAD@2"}
        (goal,) = [req for req in state.layers["requirements"] if req.id == "REQ_BTP_SAFETY_GOAL"]
        assert "REQ_BTP_CORE_LOAD@1" in goal.derived_from

    def test_an_asil_requirements_links_confirmed_withdraw_no_allocation(self, bench: Path) -> None:
        """``REQ_BTP_DECOMP_MAIN`` allocates a level to the two components linking it.

        Each pair keeps the requirement on the component, so the safety guard asks nothing and the plain approval
        commits.
        """
        seen: list[ChangeSetProposal] = []

        def review(proposal: ChangeSetProposal) -> ApprovalDecision:
            seen.append(proposal)
            return _approve(proposal)

        answer = handle_request(_change_set(bench, _confirm("REQ_BTP_DECOMP_MAIN", 2)), project=bench, review=review)
        assert answer["status"] == "committed", answer
        (proposal,) = seen
        assert proposal.safety.changes == ()
        assert {address["element"] for _, address in _server_rows(answer)} == {
            "BrakeController",
            "BrakeTemperatureSensor",
        }

    def test_a_current_link_is_untouched(self, planted: Path) -> None:
        """Confirmed at version 1 (the current one): the ``@1`` links stand.

        The bare one and ``@2`` are confirmed, the interface's ``@2`` by its remove alone since the list holds
        ``@1``.
        """
        answer = handle_request(_change_set(planted, _confirm("REQ_BTP_CORE_LOAD", 1)), project=planted)
        assert answer["status"] == "staged", answer
        assert _rows(answer) == [
            ("remove", _link("swc", "InstrumentCluster", "REQ_BTP_CORE_LOAD", "ports", "rOdometer"), "server"),
            ("create", _link("swc", "InstrumentCluster", "REQ_BTP_CORE_LOAD@1", "ports", "rOdometer"), "server"),
            ("remove", _link("interfaces", "IOdometer", "REQ_BTP_CORE_LOAD@2"), "server"),
        ]

    def test_a_malformed_suffix_and_an_element_form_member_are_left_alone(self, bench: Path) -> None:
        """Neither can be staged (``trace.link-unknown-requirement`` is an error), so the expansion is read directly."""
        from swcraft_tools.editing.link_confirmation import link_confirmations
        from swcraft_tools.snapshot import load_snapshot

        cluster = bench / "model" / "swc" / "application" / "InstrumentCluster.yaml"
        text = cluster.read_text(encoding="utf-8")
        cluster.write_text(
            text + "- REQ_BTP_CORE_LOAD@x\n- swc:REQ_BTP_CORE_LOAD\n- REQ_BTP_CORE_LOAD@1\n",
            encoding="utf-8",
            newline="\n",
        )
        snapshot = load_snapshot(str(bench / "model"))
        operation = UpdateOperation(
            address=Address(layer="requirements", element="REQ_BTP_CORE_LOAD", path=(FieldStep("version"),)),
            value=2,
            confirm_links=True,
        )
        members = [
            added.address.path[-1].member
            for added in link_confirmations(snapshot, [operation])
            if added.address.element == "InstrumentCluster"
        ]
        assert members == ["REQ_BTP_CORE_LOAD@1", "REQ_BTP_CORE_LOAD@2"]

    def test_the_rows_are_the_servers_and_the_audit_records_them_apart(self, bench: Path) -> None:
        answer = handle_request(_change_set(bench, _confirm("REQ_BTP_CORE_LOAD", 2)), project=bench)
        assert answer["status"] == "staged"
        (staged,) = _records(bench, CHANGE_SET_STAGED)
        assert staged["args"]["operations"] == [
            {
                "operation": "update",
                "address": {"layer": "requirements", "element": "REQ_BTP_CORE_LOAD", "path": [{"field": "version"}]},
            }
        ]
        server = staged["args"]["server_operations"]
        assert [row["operation"] for row in server] == ["remove", "create"] * 6
        assert {row["address"]["layer"] for row in server} == {"tasks", "hardware"}
        assert len(_server_rows(answer)) == len(server)
        (client,) = [entry for entry in answer["delta"] if "origin" not in entry]
        assert client["address"]["layer"] == "requirements"

    def test_a_link_the_client_already_replaces_is_not_rewritten_twice(self, bench: Path) -> None:
        own = [
            {"operation": "remove", "address": _link("tasks", "Task_50ms", "REQ_BTP_CORE_LOAD@1")},
            {
                "operation": "create",
                "address": _link("tasks", "Task_50ms", "REQ_BTP_CORE_LOAD@2"),
                "value": "REQ_BTP_CORE_LOAD@2",
            },
        ]
        answer = handle_request(_change_set(bench, *own, _confirm("REQ_BTP_CORE_LOAD", 2)), project=bench)
        assert answer["status"] == "staged", answer
        assert not any(address["element"] == "Task_50ms" for _, address in _server_rows(answer))
        assert len(_server_rows(answer)) == 10

    def test_without_the_flag_only_the_version_moves_and_the_links_need_confirming(self, bench: Path) -> None:
        answer = handle_request(_change_set(bench, _version("REQ_BTP_CORE_LOAD", 2)), project=bench)
        assert answer["status"] == "staged"
        assert _rows(answer) == [
            (
                "update",
                {"layer": "requirements", "element": "REQ_BTP_CORE_LOAD", "path": [{"field": "version"}]},
                None,
            )
        ]

    @pytest.mark.parametrize(
        "operation",
        [
            pytest.param(
                {**_confirm("REQ_BTP_CORE_LOAD", 2), "address": {**_confirm("X", 2)["address"], "layer": "swc"}},
                id="another-layer",
            ),
            pytest.param(
                {
                    **_confirm("REQ_BTP_CORE_LOAD", 2),
                    "address": {"layer": "requirements", "element": "REQ_BTP_CORE_LOAD", "path": [{"field": "title"}]},
                    "value": "t",
                },
                id="another-field",
            ),
            pytest.param(_confirm("REQ_BTP_CORE_LOAD", "2"), id="a-string-version"),
            pytest.param(_confirm("REQ_BTP_CORE_LOAD", True), id="a-boolean-version"),
            pytest.param(_confirm("REQ_BTP_CORE_LOAD", 0), id="version-zero"),
        ],
    )
    def test_the_flag_elsewhere_is_refused_naming_the_rule(self, bench: Path, operation: dict[str, Any]) -> None:
        answer = handle_request(_change_set(bench, operation), project=bench)
        assert answer["error"] == "schema-violation"
        assert "confirm_links applies to the update of a requirement's version" in answer["message"]
        assert _records(bench, CHANGE_SET_STAGED) == []

    @pytest.mark.parametrize("name", ["create", "remove"])
    def test_the_flag_on_another_verb_is_refused_by_the_envelope(self, bench: Path, name: str) -> None:
        operation: dict[str, Any] = {
            "operation": name,
            "address": {"layer": "swc", "element": "X"},
            "confirm_links": True,
        }
        if name == "create":
            operation["value"] = {}
        answer = handle_request(_change_set(bench, operation), project=bench)
        assert answer["error"] == "schema-violation"
        assert "carries no ['confirm_links']" in answer["message"]

    def test_a_flag_that_is_not_a_boolean_is_refused(self, bench: Path) -> None:
        answer = handle_request(
            _change_set(bench, _version("REQ_BTP_CORE_LOAD", 2, confirm_links="yes")), project=bench
        )
        assert answer["error"] == "schema-violation"
        assert "confirm_links must be a boolean" in answer["message"]

    def test_the_wire_omits_a_false_flag(self) -> None:
        address = Address(layer="requirements", element="R", path=(FieldStep("version"),))
        assert "confirm_links" not in UpdateOperation(address=address, value=2).to_json()
        assert UpdateOperation(address=address, value=2, confirm_links=True).to_json()["confirm_links"] is True

    def test_a_member_held_twice_and_removed_once_by_the_client_has_its_other_occurrence_confirmed(
        self, bench: Path
    ) -> None:
        """A ``satisfies`` list is a multiset: the client's one remove takes one occurrence, the server the other."""
        task = bench / "model" / "tasks" / "Task_50ms.yaml"
        text = task.read_text(encoding="utf-8")
        assert text.count("- REQ_BTP_CORE_LOAD@1\n") == 1
        task.write_text(
            text.replace("- REQ_BTP_CORE_LOAD@1\n", "- REQ_BTP_CORE_LOAD@1\n- REQ_BTP_CORE_LOAD@1\n"),
            encoding="utf-8",
            newline="\n",
        )
        own = {"operation": "remove", "address": _link("tasks", "Task_50ms", "REQ_BTP_CORE_LOAD@1")}
        answer = handle_request(
            _change_set(bench, own, _confirm("REQ_BTP_CORE_LOAD", 2)), project=bench, review=_approve
        )
        assert answer["status"] == "committed", answer
        rows = [
            (operation, address) for operation, address in _server_rows(answer) if address["element"] == "Task_50ms"
        ]
        assert rows == [
            ("remove", _link("tasks", "Task_50ms", "REQ_BTP_CORE_LOAD@1")),
            ("create", _link("tasks", "Task_50ms", "REQ_BTP_CORE_LOAD@2")),
        ]
        (committed,) = [t for t in load_model_state(bench / "model").layers["tasks"] if t.name == "Task_50ms"]
        assert [member for member in committed.satisfies if member.startswith("REQ_BTP_CORE_LOAD")] == [
            "REQ_BTP_CORE_LOAD@2"
        ]

    def test_a_client_remove_and_create_of_one_member_cancel_so_the_servers_removal_stays_the_servers(
        self, bench: Path
    ) -> None:
        """The client's counts are net, so the server's one removal stays the server's.

        The client's remove and re-create of ``@1`` cancel, so the server's one removal confirms the other occurrence,
        beside the create of ``@2``. The set is correctly refused (the re-created ``@1`` beside the server's ``@2`` is a
        ``trace.duplicate-link``), so the server's rows are read from the expansion directly."""
        from swcraft_tools.editing.link_confirmation import link_confirmations
        from swcraft_tools.snapshot import load_snapshot

        task = bench / "model" / "tasks" / "Task_50ms.yaml"
        text = task.read_text(encoding="utf-8")
        task.write_text(
            text.replace("- REQ_BTP_CORE_LOAD@1\n", "- REQ_BTP_CORE_LOAD@1\n- REQ_BTP_CORE_LOAD@1\n"),
            encoding="utf-8",
            newline="\n",
        )
        member = _link("tasks", "Task_50ms", "REQ_BTP_CORE_LOAD@1")
        address = Address(
            layer="tasks", element="Task_50ms", path=(FieldStep("satisfies"), MemberStep("REQ_BTP_CORE_LOAD@1"))
        )
        version = Address(layer="requirements", element="REQ_BTP_CORE_LOAD", path=(FieldStep("version"),))
        server = link_confirmations(
            load_snapshot(str(bench / "model")),
            (
                RemoveOperation(address=address),
                CreateOperation(address=address, value="REQ_BTP_CORE_LOAD@1"),
                UpdateOperation(address=version, value=2, confirm_links=True),
            ),
        )
        rows = [
            (type(operation).__name__, operation.address.path[-1].member)
            for operation in server
            if operation.address.element == "Task_50ms"
        ]
        assert rows == [("RemoveOperation", "REQ_BTP_CORE_LOAD@1"), ("CreateOperation", "REQ_BTP_CORE_LOAD@2")]

        answer = handle_request(
            _change_set(
                bench,
                {"operation": "remove", "address": member},
                {"operation": "create", "address": member, "value": "REQ_BTP_CORE_LOAD@1"},
                _confirm("REQ_BTP_CORE_LOAD", 2),
            ),
            project=bench,
        )
        assert answer["error"] == "consistency-violation", answer
        assert {"layer": "tasks", "element": "Task_50ms"} in [
            {"layer": entry["layer"], "element": entry["element"]} for entry in answer["addresses"]
        ]

    @pytest.mark.parametrize("second_flagged", [True, False], ids=["both-flagged", "one-flagged"])
    def test_two_updates_of_one_requirements_version_with_the_flag_are_refused(
        self, bench: Path, second_flagged: bool
    ) -> None:
        """The links would be confirmed at one version and the requirement end at another: refused, not guessed."""
        second = _confirm("REQ_BTP_CORE_LOAD", 3) if second_flagged else _version("REQ_BTP_CORE_LOAD", 3)
        answer = handle_request(_change_set(bench, _confirm("REQ_BTP_CORE_LOAD", 2), second), project=bench)
        assert answer["error"] == "schema-violation", answer
        assert "states requirement 'REQ_BTP_CORE_LOAD''s version more than once" in answer["message"]
        assert answer["addresses"] == [_confirm("REQ_BTP_CORE_LOAD", 2)["address"]]
        assert _records(bench, CHANGE_SET_STAGED) == []

    def test_the_refusal_is_its_own_class_under_the_flags_refusal(self) -> None:
        """``VersionStatedTwice`` owns the sentence; the engine's ``schema-violation`` arm catches its base."""
        from swcraft_tools.editing.link_confirmation import (
            LinkConfirmationNotApplicable,
            VersionStatedTwice,
            link_confirmations,
        )

        address = Address(layer="requirements", element="R", path=(FieldStep("version"),))
        operations = (
            UpdateOperation(address=address, value=2, confirm_links=True),
            UpdateOperation(address=address, value=3),
        )
        with pytest.raises(VersionStatedTwice, match="states requirement 'R''s version more than once") as refused:
            link_confirmations(None, operations)
        assert isinstance(refused.value, LinkConfirmationNotApplicable)
        assert refused.value.address == address
        with pytest.raises(LinkConfirmationNotApplicable, match="applies to the update of a requirement's version"):
            link_confirmations(None, (UpdateOperation(address=address, value=True, confirm_links=True),))

    def test_two_flagged_updates_of_two_requirements_confirm_both(self, bench: Path) -> None:
        answer = handle_request(
            _change_set(bench, _confirm("REQ_BTP_CORE_LOAD", 2), _confirm("REQ_BTP_TEMPERATURE_RANGE", 2)),
            project=bench,
        )
        assert answer["status"] == "staged", answer
        (staged,) = _records(bench, CHANGE_SET_STAGED)
        members = [row["address"]["path"][-1]["member"] for row in staged["args"]["server_operations"]]
        assert members[:12] == ["REQ_BTP_CORE_LOAD@1", "REQ_BTP_CORE_LOAD@2"] * 6
        assert members[12:] == ["REQ_BTP_TEMPERATURE_RANGE@1", "REQ_BTP_TEMPERATURE_RANGE@2"] * 3
        assert len(_server_rows(answer)) == 18

    def test_two_stale_members_of_one_list_are_two_removes_and_one_create(self, planted: Path) -> None:
        """``IOdometer`` holds ``@1`` and ``@2``; confirmed at 3, both go and ``@3`` is created once, after ``@1``'s."""
        answer = handle_request(_change_set(planted, _confirm("REQ_BTP_CORE_LOAD", 3)), project=planted)
        assert answer["status"] == "staged", answer
        staged = _records(planted, CHANGE_SET_STAGED)[-1]  # the plant's own set is the first
        rows = [
            (row["operation"], row["address"])
            for row in staged["args"]["server_operations"]
            if row["address"]["element"] == "IOdometer"
        ]
        assert rows == [
            ("remove", _link("interfaces", "IOdometer", "REQ_BTP_CORE_LOAD@1")),
            ("create", _link("interfaces", "IOdometer", "REQ_BTP_CORE_LOAD@3")),
            ("remove", _link("interfaces", "IOdometer", "REQ_BTP_CORE_LOAD@2")),
        ]

    def test_a_link_the_client_creates_at_the_new_version_takes_the_servers_remove_alone(self, bench: Path) -> None:
        own = {
            "operation": "create",
            "address": _link("tasks", "Task_50ms", "REQ_BTP_CORE_LOAD@2"),
            "value": "REQ_BTP_CORE_LOAD@2",
        }
        answer = handle_request(_change_set(bench, own, _confirm("REQ_BTP_CORE_LOAD", 2)), project=bench)
        assert answer["status"] == "staged", answer
        rows = [
            (operation, address) for operation, address in _server_rows(answer) if address["element"] == "Task_50ms"
        ]
        assert rows == [("remove", _link("tasks", "Task_50ms", "REQ_BTP_CORE_LOAD@1"))]

    def test_a_confirmation_with_nothing_to_confirm_stages_an_empty_delta(self, bench: Path) -> None:
        """``REQ_BTP_DIAGNOSTICS`` is at 1 and every link names ``@1``: no row, as an update to the value held."""
        answer = handle_request(_change_set(bench, _confirm("REQ_BTP_DIAGNOSTICS", 1)), project=bench)
        assert answer["status"] == "staged", answer
        assert answer["delta"] == []
        unflagged = handle_request(_change_set(bench, _version("REQ_BTP_DIAGNOSTICS", 1)), project=bench)
        assert unflagged["delta"] == []

    def test_a_flagged_update_naming_no_requirement_is_a_broken_reference(self, bench: Path) -> None:
        answer = handle_request(_change_set(bench, _confirm("REQ_NOWHERE", 2)), project=bench)
        assert answer["error"] == "broken-reference", answer
        assert answer["addresses"] == [_confirm("REQ_NOWHERE", 2)["address"]]

    def test_an_unparseable_model_file_is_a_consistency_violation(self, bench: Path) -> None:
        (bench / "model" / "hardware" / "Broken.yaml").write_text("name: [\n", encoding="utf-8", newline="\n")
        answer = handle_request(_change_set(bench, _confirm("REQ_BTP_CORE_LOAD", 2)), project=bench)
        assert answer["error"] == "consistency-violation"
        assert "'hardware' element 'Broken'" in answer["message"]
        assert ".yaml" not in answer["message"]
        assert str(bench) not in answer["message"]


class TestTheThreeExpansionsInOrder:
    """``with_dependents``, ``confirm_links`` and ``complete`` in one set: dependents, then links, then repairs."""

    @pytest.fixture
    def basic(self, tmp_path: Path) -> Path:
        import shutil

        from conftest import REPO_ROOT

        root = tmp_path / "basic"
        shutil.copytree(REPO_ROOT / "examples" / "basic", root, ignore=shutil.ignore_patterns("dist", "views"))
        model = root / "model"
        for relative, text in (
            ("data_types/application/Temp_C.yaml", "name: Temp_C\nkind: application_type\n"),
            (
                "interfaces/ITemp.yaml",
                "name: ITemp\nkind: sender_receiver\ndata_elements:\n  - name: t\n    application_type: Temp_C\n",
            ),
            (
                "data_types/mapping_sets/Thermal.yaml",
                "name: Thermal\nmaps:\n  - application: Temp_C\n    implementation: float32\n",
            ),
        ):
            (model / relative).parent.mkdir(parents=True, exist_ok=True)
            (model / relative).write_text(text, encoding="utf-8", newline="\n")
        return root

    def test_dependents_then_links_then_repairs(self, basic: Path) -> None:
        logger = {
            "name": "Logger",
            "group": "example",
            "type": "application",
            "ports": [{"name": "rTemp", "direction": "require", "interface": "ITemp"}],
            "memory": {"RAM0": 0},
            "handle_termination_and_restart": "no_support",
        }
        operations = (
            {"operation": "create", "address": {"layer": "swc", "element": "Logger"}, "value": logger},
            {
                "operation": "remove",
                "address": {"layer": "swc", "element": "ExampleConsumerSWC"},
                "with_dependents": True,
            },
            _confirm("REQ_EXAMPLE_001", 2),
        )
        answer = handle_request(_change_set(basic, *operations, complete=True), project=basic)
        assert answer["status"] == "staged", answer
        (staged,) = _records(basic, CHANGE_SET_STAGED)
        server = [
            (row["operation"], row["address"]["layer"], row["address"]["element"])
            for row in staged["args"]["server_operations"]
        ]
        assert server == [
            ("remove", "connectors", "Example_to_Consumer"),
            ("remove", "compositions", "Root"),
            ("remove", "tasks", "Task_10ms"),
            ("remove", "sequences", "task10ms_consumer_cycle"),
            # ExampleConsumerSWC's own link goes with the component the client removes: not rewritten.
            ("remove", "swc", "ExampleSWC"),
            ("create", "swc", "ExampleSWC"),
            ("create", "swc", "Logger"),
        ]


#: The requirement the bounded-links tests link from many interfaces (copies of one that links it).
_HUB_REQUIREMENT = "REQ_BTP_TEMPERATURE_RANGE"
#: Interfaces added to the bench, each linking the hub: past the bound, and past the response
#: budget had the list stayed unbounded (each link is about 270 bytes on the wire).
_HUB_LINKS_ADDED = 400


def _link_the_hub_from_many_interfaces(project: Path) -> None:
    template = project / "model" / "interfaces" / "IBrakeDiagnostics.yaml"
    text = template.read_text(encoding="utf-8")
    for index in range(_HUB_LINKS_ADDED):
        name = f"IHubLink{index:03d}"
        body = text.replace("name: IBrakeDiagnostics\n", f"name: {name}\n", 1)
        (template.parent / f"{name}.yaml").write_text(body, encoding="utf-8", newline="\n")


class TestFindForRequirementBoundsTheExistingLinks:
    """``existing_links`` is bounded, so a hub requirement is answered rather than refused.

    ``existing_links_more`` counts the rest and ``existing_links_read`` is followed to the end.
    """

    @pytest.fixture
    def hub(self, bench: Path) -> Path:
        _link_the_hub_from_many_interfaces(bench)
        return bench

    def test_the_answer_lists_the_bound_counts_the_rest_and_names_the_read(self, hub: Path) -> None:
        answer = _find_for(hub, _HUB_REQUIREMENT)
        assert answer["response"] == "find", answer
        every = _ranked_as_the_experiment(hub, _HUB_REQUIREMENT, FIND_CANDIDATES_DEFAULT)["existing_links"]
        assert len(every) > _HUB_LINKS_ADDED
        assert answer["existing_links"] == every[:FIND_EXISTING_LINKS_MAX]
        assert answer["existing_links_more"] == len(every) - FIND_EXISTING_LINKS_MAX
        assert answer["existing_links_read"] == {
            "operation": "read",
            "address": {"layer": "requirements"},
            "form": "index",
            "where": [{"field": "id", "equals": _HUB_REQUIREMENT}],
            "limit": HINT_PAGE_SIZE,
            "derived": ["realised_by"],
        }
        assert measure(answer) < RESPONSE_BUDGET_WIRE_BYTES
        assert measure(answer | {"existing_links": every}) > RESPONSE_BUDGET_WIRE_BYTES, "the unbounded list"
        assert FindCandidatesResponse.from_json(answer).to_json() == answer

    def test_following_the_read_returns_every_link(self, hub: Path) -> None:
        answer = _find_for(hub, _HUB_REQUIREMENT)
        request = dict(answer["existing_links_read"])
        rows: list[dict[str, Any]] = []
        while True:
            page = handle_request(request, project=hub)
            assert page["response"] == "read", page
            rows.extend(row["derived"]["realised_by"] for row in page["value"]["identities"])
            if "next" not in page["page"]:
                break
            request = dict(answer["existing_links_read"]) | {"after": page["page"]["next"]}
        every = _ranked_as_the_experiment(hub, _HUB_REQUIREMENT, FIND_CANDIDATES_DEFAULT)["existing_links"]
        assert len(rows) == len(every)
        assert sorted((row["layer"], row["element"], row["stated"]) for row in rows) == sorted(
            (link["address"]["layer"], link["address"]["element"], link["version"]) for link in every
        )

    def test_a_requirement_within_the_bound_carries_neither_key(self, bench: Path) -> None:
        answer = _find_for(bench, _HUB_REQUIREMENT)
        assert 0 < len(answer["existing_links"]) <= FIND_EXISTING_LINKS_MAX
        assert "existing_links_more" not in answer and "existing_links_read" not in answer

    def test_a_malformed_suffix_beside_the_bare_id_is_two_links_here_and_one_row_there(self, hub: Path) -> None:
        """The stated difference: the search keeps one link per member string, ``realised_by`` one row per site."""
        first = hub / "model" / "interfaces" / "IHubLink000.yaml"
        text = first.read_text(encoding="utf-8")
        both = f"- {_HUB_REQUIREMENT}@x\n- {_HUB_REQUIREMENT}\n"
        first.write_text(text.replace(f"- {_HUB_REQUIREMENT}@1\n", both, 1), encoding="utf-8", newline="\n")
        answer = _find_for(hub, _HUB_REQUIREMENT)
        request = dict(answer["existing_links_read"])
        rows: list[dict[str, Any]] = []
        while True:
            page = handle_request(request, project=hub)
            assert page["response"] == "read", page
            rows.extend(row["derived"]["realised_by"] for row in page["value"]["identities"])
            if "next" not in page["page"]:
                break
            request = dict(answer["existing_links_read"]) | {"after": page["page"]["next"]}
        assert len(answer["existing_links"]) + answer["existing_links_more"] == len(rows) + 1
        merged = [row for row in rows if row["element"] == "IHubLink000"]
        assert merged == [merged[0]] and merged[0]["stated"] is None


def _synthetic_links(count: int) -> tuple[ExistingLink, ...]:
    return tuple(
        ExistingLink(TargetAddress("interface", None, f"ILink{index:02d}"), 1, True, "REQ_X@1")
        for index in range(count)
    )


class TestProjectRankingAtTheBound:
    """:func:`project_ranking` at exactly :data:`FIND_EXISTING_LINKS_MAX` links and at one more."""

    RANKING = Ranking(requirement="REQ_X", neighbours=0, ranked=(), candidates=FIND_CANDIDATES_DEFAULT)

    def _answer(self, count: int) -> dict[str, Any]:
        response = project_ranking(
            self.RANKING, _synthetic_links(count), baseline="rev1:9f4c2a7be3d18056", for_requirement="REQ_X"
        )
        answer = response.to_json()
        assert FindCandidatesResponse.from_json(answer).to_json() == answer
        return answer

    def test_sixteen_links_carry_neither_key(self) -> None:
        answer = self._answer(FIND_EXISTING_LINKS_MAX)
        assert len(answer["existing_links"]) == FIND_EXISTING_LINKS_MAX
        assert "existing_links_more" not in answer and "existing_links_read" not in answer

    def test_seventeen_links_list_sixteen_count_one_and_name_the_read(self) -> None:
        answer = self._answer(FIND_EXISTING_LINKS_MAX + 1)
        assert [link["target"]["name"] for link in answer["existing_links"]] == [
            f"ILink{index:02d}" for index in range(FIND_EXISTING_LINKS_MAX)
        ]
        assert answer["existing_links_more"] == 1
        assert answer["existing_links_read"] == links_read("REQ_X").to_json()

"""The trace link search's two files of decisions: the proposals reader, the pending count, the decisions file.

Every proposals file here is built by ``trace_models.proposals_for`` over a
synthetic model and changed in **one** place per refusal, so each
negative is one edit away from a valid file. The schema beside the reader
(``trace_proposals.schema.json``) is held to the same files: it accepts every
positive one, refuses every schema-level negative, and **accepts** the
reader-only negatives, which the reader alone refuses -- the requirements
interchange's ``negative-reader-only/`` pattern.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from swcraft_tools import trace_records
from swcraft_tools.project import require_project
from swcraft_tools.trace_records import (
    DECISIONS_FILE,
    DecisionRecord,
    TraceRecordsError,
    decisions_text,
    merge_decisions,
    no_decisions,
    parse_decisions,
    parse_proposals,
    pending,
    read_decisions,
    read_decisions_bytes,
    read_proposals,
    rejected_pairs,
)
from swcraft_tools.trace_search import TargetAddress, TraceExperimentError, exact_decimal
from trace_models import TraceModel, port, proposals_for, runnable, write_project, write_proposals

CTL = TargetAddress("swc", None, "Ctl")
OUT = TargetAddress("port", "Ctl", "pOut")
STEP = TargetAddress("runnable", "Ctl", "Step")
TOP = TargetAddress("composition", None, "Top")


def _model() -> TraceModel:
    """SR-1 at version 2, linked to ``Ctl`` at version 1 (awaiting confirmation); SR-2 linked to ``Ctl``, confirmed."""
    model = TraceModel()
    model.requirement("SR-1", "Brake temperature", "Supervise the brake temperature", version=2)
    model.requirement("SR-2", "Door lock", "Doors shall lock")
    model.component(
        "Ctl",
        description="brake control",
        satisfies=["SR-1@1", "SR-2@1"],
        ports=[port("pOut")],
        runnables=[runnable("Step")],
    )
    model.composition("Top", description="the top composition")
    return model


@pytest.fixture(scope="module")
def valid(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """A valid file.

    SR-1 with an accept row, a reject row and its awaiting link; SR-2 with an undecided row and a chosen entry."""
    root = _model().write(tmp_path_factory.mktemp("records") / "model")
    return proposals_for(
        root,
        {"SR-1": [(OUT, "accept"), (TOP, "reject")], "SR-2": [(STEP, None)]},
        chosen={"SR-2": [TOP]},
    )


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads((resource_files("swcraft_tools") / trace_records.SCHEMA_RESOURCE).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(document, indent=2).encode("utf-8")


def _refusal(document: dict[str, Any]) -> TraceRecordsError:
    with pytest.raises(TraceRecordsError) as caught:
        parse_proposals(_bytes(document), "proposals.json")
    return caught.value


def _edited(valid: dict[str, Any], edit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    document = copy.deepcopy(valid)
    edit(document)
    return document


def _candidate(document: dict[str, Any], entry: int, row: int) -> dict[str, Any]:
    return document["requirements"][entry]["candidates"][row]


def _link(document: dict[str, Any], entry: int) -> dict[str, Any]:
    return document["requirements"][entry]["linked"][0]


# ---------------------------------------------------------------------------
# The valid file
# ---------------------------------------------------------------------------


class TestAValidFile:
    def test_it_is_read(self, valid: dict[str, Any], tmp_path: Path) -> None:
        path = write_proposals(tmp_path / "proposals.json", valid)
        proposals = read_proposals(path)
        assert proposals.document == valid
        assert proposals.digest.startswith("sha256:") and len(proposals.digest) == 71
        first, second = proposals.model.requirements
        assert [row.decision for row in first.candidates] == ["accept", "reject"]
        assert first.candidates[0].target.target() == OUT
        assert first.linked[0].member == "SR-1@1" and first.linked[0].decision is None
        assert second.linked[0].confirmed and second.chosen[0].target.target() == TOP

    def test_the_built_linked_entries_carry_their_slots(self, valid: dict[str, Any]) -> None:
        assert _link(valid, 0) == {
            "target": CTL.to_json(),
            "member": "SR-1@1",
            "version": 1,
            "confirmed": False,
            "decision": None,
        }
        assert "decision" not in _link(valid, 1)

    def test_the_digest_is_of_the_bytes(self, valid: dict[str, Any]) -> None:
        import hashlib

        data = _bytes(valid)
        read = parse_proposals(data, "f")
        assert read.digest == f"sha256:{hashlib.sha256(data).hexdigest()}"
        assert read.data == data

    def test_marks_and_a_confirm_are_read(self, valid: dict[str, Any]) -> None:
        def edit(document: dict[str, Any]) -> None:
            _candidate(document, 0, 0).update(applied=True, preselected=True)
            _candidate(document, 0, 1)["stale"] = "SR-1 changed since the search (same version)"
            _link(document, 0)["decision"] = "confirm"
            document["requirements"][1]["chosen"][0]["applied"] = True

        model = parse_proposals(_bytes(_edited(valid, edit)), "f").model
        assert model.requirements[0].candidates[0].applied is True
        assert model.requirements[0].linked[0].decision == "confirm"

    def test_a_refusal_naming_a_version_of_any_width_names_it_exactly(self, valid: dict[str, Any]) -> None:
        wide = exact_decimal(10**5000 + 1)  # 5,001 digits, wider than ``str()``'s default limit
        sentinel = 987654321

        def wide_bytes(document: dict[str, Any]) -> bytes:
            return json.dumps(document).replace(str(sentinel), wide).encode("utf-8")

        document = _edited(valid, lambda d: _link(d, 0).update(member="SR-1@1", version=sentinel))
        refusal = _refusal_of(wide_bytes(document))
        assert refusal.pointer == "/requirements/0/linked/0/version"
        assert refusal.sentence == f"the member 'SR-1@1' names version 1, not {wide}"
        document = _edited(valid, lambda d: d["requirements"][0].update(version=sentinel))
        _link(document, 0).update(member=f"SR-1@{sentinel}", version=sentinel)
        refusal = _refusal_of(wide_bytes(document))
        assert refusal.pointer == "/requirements/0/linked/0/confirmed"
        assert refusal.sentence == (
            f"the member 'SR-1@{wide}' names the requirement's version {wide}, so 'confirmed' is true"
        )

    def test_a_version_of_any_width_is_read_exactly(self, valid: dict[str, Any]) -> None:
        wide = 10**5000 + 1
        text = _bytes(valid).decode("utf-8").replace('"version": 2,', f'"version": {exact_decimal(wide)},', 1)
        assert parse_proposals(text.encode("utf-8"), "f").model.requirements[0].version == wide

    def test_a_refusal_names_the_file_as_given(self, valid: dict[str, Any], tmp_path: Path) -> None:
        path = write_proposals(tmp_path / "proposals.json", _edited(valid, lambda d: d.update(kind="allocation")))
        with pytest.raises(TraceRecordsError) as caught:
            read_proposals(path, "dist/experiments/trace/proposals.json")
        assert str(caught.value) == "dist/experiments/trace/proposals.json: /kind: Input should be 'trace'"
        assert isinstance(caught.value, TraceExperimentError)

    def test_an_absent_file_is_refused_by_its_given_name(self, tmp_path: Path) -> None:
        with pytest.raises(TraceRecordsError, match=r"^gone\.json: \(top level\): cannot be read"):
            read_proposals(tmp_path / "gone.json", "gone.json")

    def test_text_that_is_no_json_or_no_utf8_is_refused(self) -> None:
        assert _pointer_of(b"{", "") and "is not JSON" in str(_refusal_of(b"{"))
        assert "is not UTF-8" in str(_refusal_of(b'{"a": "\xff"}'))
        assert "must be a JSON object" in str(_refusal_of(b"[]"))
        assert "NaN is not a JSON value" in str(_refusal_of(b'{"a": NaN}'))


def _refusal_of(data: bytes) -> TraceRecordsError:
    with pytest.raises(TraceRecordsError) as caught:
        parse_proposals(data, "f")
    return caught.value


def _pointer_of(data: bytes, pointer: str) -> bool:
    return _refusal_of(data).pointer == pointer


# ---------------------------------------------------------------------------
# The refusals, each one change away from the valid file
# ---------------------------------------------------------------------------


def _set(*path: Any, value: Any) -> Callable[[dict[str, Any]], None]:
    def edit(document: dict[str, Any]) -> None:
        node: Any = document
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return edit


def _drop(*path: Any) -> Callable[[dict[str, Any]], None]:
    def edit(document: dict[str, Any]) -> None:
        node: Any = document
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return edit


def _append(*path: Any, value: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], None]:
    def edit(document: dict[str, Any]) -> None:
        node: Any = document
        for key in path:
            node = node[key]
        node.append(value(document))

    return edit


R0, R1 = ("requirements", 0), ("requirements", 1)

#: Refused by the schema and the reader alike: (name, edit, pointer).
SCHEMA_NEGATIVES: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("a key outside the format, top level", _set("extra", value=1), "/extra"),
    (
        "a key outside the format, in a row",
        _set(*R0, "candidates", 0, "note", value="x"),
        "/requirements/0/candidates/0/note",
    ),
    (
        "a key outside the format, in an address",
        _set(*R0, "candidates", 0, "target", "group", value="x"),
        "/requirements/0/candidates/0/target/group",
    ),
    ("a wrong type", _set(*R0, "version", value="2"), "/requirements/0/version"),
    ("a boolean for a count", _set(*R0, "version", value=True), "/requirements/0/version"),
    ("a missing key", _drop(*R0, "fingerprint"), "/requirements/0/fingerprint"),
    ("no title", _drop(*R0, "title"), "/requirements/0/title"),
    ("no description", _drop(*R0, "description"), "/requirements/0/description"),
    ("a title that is not text", _set(*R0, "title", value=7), "/requirements/0/title"),
    ("formatVersion 2", _set("formatVersion", value=2), "/formatVersion"),
    ("formatVersion true", _set("formatVersion", value=True), "/formatVersion"),
    ("kind other than trace", _set("kind", value="allocation"), "/kind"),
    (
        "a decision outside the vocabulary",
        _set(*R0, "candidates", 0, "decision", value="maybe"),
        "/requirements/0/candidates/0/decision",
    ),
    (
        "a confirm on a row",
        _set(*R0, "candidates", 0, "decision", value="confirm"),
        "/requirements/0/candidates/0/decision",
    ),
    ("an accept on a link", _set(*R0, "linked", 0, "decision", value="accept"), "/requirements/0/linked/0/decision"),
    ("a confirm on a confirmed link", _set(*R1, "linked", 0, "decision", value="confirm"), "/requirements/1/linked/0"),
    ("a null decision on a confirmed link", _set(*R1, "linked", 0, "decision", value=None), "/requirements/1/linked/0"),
    (
        "a link awaiting confirmation with no decision slot",
        _drop(*R0, "linked", 0, "decision"),
        "/requirements/0/linked/0",
    ),
    ("applied on an undecided row", _set(*R1, "candidates", 0, "applied", value=True), "/requirements/1/candidates/0"),
    ("stale on an undecided row", _set(*R1, "candidates", 0, "stale", value="gone"), "/requirements/1/candidates/0"),
    ("applied on an undecided link", _set(*R0, "linked", 0, "applied", value=True), "/requirements/0/linked/0"),
    (
        "applied and stale on one row",
        lambda d: _candidate(d, 0, 0).update(applied=True, stale="gone"),
        "/requirements/0/candidates/0",
    ),
    (
        "applied and stale on a chosen entry",
        lambda d: d["requirements"][1]["chosen"][0].update(applied=True, stale="gone"),
        "/requirements/1/chosen/0",
    ),
    ("applied false", _set(*R0, "candidates", 0, "applied", value=False), "/requirements/0/candidates/0/applied"),
    ("applied 1", _set(*R0, "candidates", 0, "applied", value=1), "/requirements/0/candidates/0/applied"),
    (
        "preselected false",
        _set(*R0, "candidates", 0, "preselected", value=False),
        "/requirements/0/candidates/0/preselected",
    ),
    ("applied null", _set(*R0, "candidates", 0, "applied", value=None), "/requirements/0/candidates/0"),
    ("preselected on a reject", _set(*R0, "candidates", 1, "preselected", value=True), "/requirements/0/candidates/1"),
    (
        "preselected on an undecided row",
        _set(*R1, "candidates", 0, "preselected", value=True),
        "/requirements/1/candidates/0",
    ),
    ("stale null on a link", _set(*R0, "linked", 0, "stale", value=None), "/requirements/0/linked/0"),
    ("an empty stale reason", _set(*R0, "candidates", 0, "stale", value=""), "/requirements/0/candidates/0/stale"),
    (
        "a kind not one of the ten",
        _set(*R0, "candidates", 0, "target", "kind", value="connector"),
        "/requirements/0/candidates/0/target/kind",
    ),
    (
        "an owner on a kind that has none",
        _set(*R0, "candidates", 1, "target", "owner", value="Ctl"),
        "/requirements/0/candidates/1/target",
    ),
    (
        "no owner on a port",
        _set(*R0, "candidates", 0, "target", "owner", value=None),
        "/requirements/0/candidates/0/target",
    ),
    (
        "no owner on a runnable",
        _set(*R1, "candidates", 0, "target", "owner", value=None),
        "/requirements/1/candidates/0/target",
    ),
    (
        "a chosen entry with a decision",
        _set(*R1, "chosen", 0, "decision", value="accept"),
        "/requirements/1/chosen/0/decision",
    ),
    (
        "a wrong score denominator",
        _set(*R0, "candidates", 0, "score", "d", value=1000),
        "/requirements/0/candidates/0/score/d",
    ),
]

#: Refused by the reader alone: JSON Schema cannot compare across objects.
READER_ONLY_NEGATIVES: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("a requirement id in two entries", _set(*R1, "id", value="SR-1"), "/requirements/1/id"),
    (
        "a pair in two rows",
        _set(*R0, "candidates", 1, "target", value=OUT.to_json()),
        "/requirements/0/candidates/1/target",
    ),
    (
        "a pair in a row and a chosen entry",
        _set(*R0, "chosen", value=[{"target": OUT.to_json()}]),
        "/requirements/0/chosen/0/target",
    ),
    (
        "a pair in two chosen entries",
        _append(*R1, "chosen", value=lambda d: {"target": TOP.to_json()}),
        "/requirements/1/chosen/1/target",
    ),
    (
        "a member naming another requirement",
        _set(*R0, "linked", 0, "member", value="SR-2@1"),
        "/requirements/0/linked/0/member",
    ),
    (
        "a member in the element form",
        _set(*R0, "linked", 0, "member", value="compositions:Top"),
        "/requirements/0/linked/0/member",
    ),
    (
        "a member disagreeing with version",
        _set(*R0, "linked", 0, "version", value=3),
        "/requirements/0/linked/0/version",
    ),
    (
        "a bare member with a version",
        _set(*R0, "linked", 0, "member", value="SR-1"),
        "/requirements/0/linked/0/version",
    ),
    (
        "a confirm whose member already names the recorded version",
        lambda d: _link(d, 0).update(member="SR-1@2", version=2, decision="confirm"),
        "/requirements/0/linked/0/confirmed",
    ),
    (
        "a link in two linked entries",
        _append(*R0, "linked", value=lambda d: copy.deepcopy(_link(d, 0))),
        "/requirements/0/linked/1/member",
    ),
    ("an integer written with a fraction", _set("model", "files", value=1.0), "/model/files"),
    ("a version written with a fraction", _set(*R0, "version", value=2.0), "/requirements/0/version"),
]


class TestTheRefusals:
    @pytest.mark.parametrize(("name", "edit", "pointer"), SCHEMA_NEGATIVES, ids=[case[0] for case in SCHEMA_NEGATIVES])
    def test_a_schema_level_negative(
        self, valid: dict[str, Any], validator: Draft202012Validator, name: str, edit, pointer: str
    ) -> None:
        document = _edited(valid, edit)
        assert _refusal(document).pointer == pointer
        assert list(validator.iter_errors(document)), f"the schema accepts {name}"

    @pytest.mark.parametrize(
        ("name", "edit", "pointer"), READER_ONLY_NEGATIVES, ids=[case[0] for case in READER_ONLY_NEGATIVES]
    )
    def test_a_reader_only_negative(
        self, valid: dict[str, Any], validator: Draft202012Validator, name: str, edit, pointer: str
    ) -> None:
        document = _edited(valid, edit)
        assert _refusal(document).pointer == pointer
        assert list(validator.iter_errors(document)) == [], f"the schema refuses {name}: it is the reader's alone"

    def test_a_repeated_key_is_reader_only(self, valid: dict[str, Any], validator: Draft202012Validator) -> None:
        text = _bytes(valid).decode("utf-8").replace('"kind": "trace",', '"kind": "trace", "kind": "trace",', 1)
        refusal = _refusal_of(text.encode("utf-8"))
        assert (refusal.pointer, refusal.sentence) == ("/kind", "the key appears twice in one object")
        assert list(validator.iter_errors(json.loads(text))) == []

    def test_the_first_problem_is_reported(self, valid: dict[str, Any]) -> None:
        def two(document: dict[str, Any]) -> None:
            _set(*R1, "id", value="SR-1")(document)
            _set(*R0, "linked", 0, "member", value="SR-2@1")(document)

        assert _refusal(_edited(valid, two)).pointer == "/requirements/0/linked/0/member"

    def test_the_sentences_name_the_rule(self, valid: dict[str, Any]) -> None:
        assert _refusal(_edited(valid, _set(*R1, "id", value="SR-1"))).sentence == (
            "'SR-1' has an entry already (requirements/0): one entry per requirement"
        )
        assert _refusal(_edited(valid, _set(*R0, "linked", 0, "member", value="SR-2@1"))).sentence == (
            "'SR-2@1' names 'SR-2', not this entry's requirement 'SR-1'"
        )
        assert _refusal(_edited(valid, _set(*R0, "candidates", 0, "target", "owner", value=None))).sentence == (
            "a port names its component in 'owner'"
        )
        assert _refusal(_edited(valid, _set("extra", value=1))).sentence == "is not a key of the format"
        duplicate = _append(*R0, "linked", value=lambda d: copy.deepcopy(_link(d, 0)))
        assert _refusal(_edited(valid, duplicate)).sentence == (
            "the link 'SR-1@1' on this target is in linked/0 already: one linked entry per target and member"
        )
        assert _refusal(_edited(valid, _set("model", "files", value=1.0))).sentence == "must be an integer"
        assert _refusal(_edited(valid, _set(*R0, "candidates", 1, "preselected", value=True))).sentence == (
            "'preselected' marks an accept made from a preselection: a row not accepted carries none"
        )

    def test_a_model_digest_that_differs_is_no_refusal(self, valid: dict[str, Any]) -> None:
        document = _edited(valid, _set("model", "digest", value="sha256:" + "f" * 64))
        assert parse_proposals(_bytes(document), "f").model.model.digest.endswith("f" * 64)


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------


class TestTheSchema:
    def test_it_is_package_data_beside_the_reader(self) -> None:
        resource = resource_files("swcraft_tools") / trace_records.SCHEMA_RESOURCE
        assert resource.is_file()
        assert Path(trace_records.__file__).with_name(trace_records.SCHEMA_RESOURCE).is_file()

    def test_it_accepts_every_positive_file(self, valid: dict[str, Any], validator: Draft202012Validator) -> None:
        def decided(document: dict[str, Any]) -> None:
            _candidate(document, 0, 0).update(applied=True, preselected=True)
            _candidate(document, 0, 1)["stale"] = "gone"
            _link(document, 0)["decision"] = "confirm"
            document["requirements"][1]["chosen"][0]["stale"] = "an external stub is never linked"

        for document in (valid, _edited(valid, decided)):
            assert list(validator.iter_errors(document)) == []
            parse_proposals(_bytes(document), "f")

    def test_it_is_closed(self, validator: Draft202012Validator) -> None:
        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node.get("additionalProperties") is False, node
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(validator.schema)


# ---------------------------------------------------------------------------
# Pending work
# ---------------------------------------------------------------------------


class TestPending:
    def _count(self, valid: dict[str, Any], edit: Callable[[dict[str, Any]], None] | None = None) -> int:
        document = valid if edit is None else _edited(valid, edit)
        return pending(parse_proposals(_bytes(document), "f").model)

    def test_each_kind_of_item(self, valid: dict[str, Any]) -> None:
        # The valid file: an accept and a reject row and a chosen entry are pending; the undecided row and link are not.
        assert self._count(valid) == 3
        assert self._count(valid, _set(*R0, "linked", 0, "decision", value="confirm")) == 4

    def test_applied_and_stale_are_not_pending(self, valid: dict[str, Any]) -> None:
        def done(document: dict[str, Any]) -> None:
            _candidate(document, 0, 0)["applied"] = True
            _candidate(document, 0, 1)["stale"] = "gone"
            document["requirements"][1]["chosen"][0]["applied"] = True
            _link(document, 0).update(decision="confirm", stale="the link changed since the search")

        assert self._count(valid, done) == 0

    def test_a_file_with_nothing_decided(self, valid: dict[str, Any]) -> None:
        def undecided(document: dict[str, Any]) -> None:
            for entry in document["requirements"]:
                entry["chosen"] = []
                for row in entry["candidates"]:
                    row["decision"] = None

        assert self._count(valid, undecided) == 0


# ---------------------------------------------------------------------------
# The decisions file
# ---------------------------------------------------------------------------


def _record(requirement: str, version: int, target: TargetAddress, decision: str, **slots: Any) -> DecisionRecord:
    return DecisionRecord.model_validate(
        {"requirement": requirement, "version": version, "target": target.to_json(), "decision": decision, **slots}
    )


class TestTheDecisionsFile:
    def test_the_order_and_the_text(self) -> None:
        records = [
            _record("sr-3", 1, TOP, "reject"),
            _record("SR-2", 2, OUT, "accept", preselected=True),
            _record("SR-2", 2, CTL, "reject"),
            _record("SR-2", 1, STEP, "confirm", **{"from": "SR-2"}),
            _record("SR-10", 1, TOP, "accept"),
        ]
        text = decisions_text(merge_decisions(no_decisions(), records))
        assert text == (
            "formatVersion: 1\n"
            "decisions:\n"
            "- requirement: SR-10\n  version: 1\n  target:\n    kind: composition\n    owner: null\n    name: Top\n"
            "  decision: accept\n"
            "- requirement: SR-2\n  version: 1\n  target:\n    kind: runnable\n    owner: Ctl\n    name: Step\n"
            "  decision: confirm\n  from: SR-2\n"
            "- requirement: SR-2\n  version: 2\n  target:\n    kind: swc\n    owner: null\n    name: Ctl\n"
            "  decision: reject\n"
            "- requirement: SR-2\n  version: 2\n  target:\n    kind: port\n    owner: Ctl\n    name: pOut\n"
            "  decision: accept\n  preselected: true\n"
            "- requirement: sr-3\n  version: 1\n  target:\n    kind: composition\n    owner: null\n    name: Top\n"
            "  decision: reject\n"
        )
        assert parse_decisions(text) == merge_decisions(no_decisions(), records)

    def test_one_record_per_key_the_later_replacing(self) -> None:
        held = merge_decisions(no_decisions(), [_record("SR-1", 2, OUT, "reject"), _record("SR-1", 1, OUT, "reject")])
        merged = merge_decisions(held, [_record("SR-1", 2, OUT, "accept", preselected=True)])
        assert [(r.version, r.decision, r.preselected) for r in merged.decisions] == [
            (1, "reject", None),
            (2, "accept", True),
        ]
        again = merge_decisions(merged, [_record("SR-1", 2, OUT, "reject")])
        assert [r.decision for r in again.decisions] == ["reject", "reject"]

    def test_two_writes_of_the_same_records_are_byte_equal(self) -> None:
        records = [_record("SR-2", 1, TOP, "reject"), _record("SR-1", 2, OUT, "accept")]
        first = decisions_text(merge_decisions(no_decisions(), records))
        second = decisions_text(merge_decisions(parse_decisions(first), reversed(records)))
        assert first == second

    def test_rejected_pairs(self) -> None:
        decisions = merge_decisions(
            no_decisions(), [_record("SR-1", 2, OUT, "reject"), _record("SR-1", 2, TOP, "accept")]
        )
        assert rejected_pairs(decisions) == frozenset({("SR-1", 2, OUT)})

    @pytest.mark.parametrize(
        ("slots", "sentence"),
        [
            ({"decision": "reject", "preselected": True}, "'preselected' is recorded on an accept only"),
            ({"decision": "accept", "preselected": False}, "must be true"),
            ({"decision": "accept", "from": "SR-1@1"}, "'from' is recorded on a confirm only"),
            ({"decision": "confirm"}, "a confirm records the member it replaced in 'from'"),
            ({"decision": "confirm", "from": None}, "'from' is never null: omit the key instead"),
            ({"decision": "confirm", "from": "SR-9@1"}, "'from' is 'SR-9@1', a member that does not name 'SR-1'"),
        ],
    )
    def test_preselected_and_from_only_where_allowed(self, slots: dict[str, Any], sentence: str) -> None:
        record = {"requirement": "SR-1", "version": 2, "target": OUT.to_json(), **slots}
        text = decisions_text(no_decisions()).replace("decisions: []\n", "decisions:\n- " + json.dumps(record) + "\n")
        with pytest.raises(TraceRecordsError) as caught:
            parse_decisions(text)
        assert caught.value.file == DECISIONS_FILE and caught.value.pointer.startswith("/decisions/0")
        assert caught.value.sentence == sentence

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            (
                "formatVersion: 1\ndecisions:\n- requirement: SR-1\n  version: x\n",
                "/decisions/0/version: must be an integer",
            ),
            ("formatVersion: 2\ndecisions: []\n", "/formatVersion: must be 1"),
            ("decisions: []\n", "/formatVersion: is required"),
            ("formatVersion: 1\ndecisions: []\nnote: x\n", "/note: is not a key of the format"),
            ("- a\n", "(top level): must be a mapping with 'formatVersion' and 'decisions'"),
            ("formatVersion: [1\n", "(top level): is not YAML (line 2, column 1)"),
        ],
    )
    def test_a_malformed_file_is_refused_naming_its_place(self, text: str, message: str) -> None:
        with pytest.raises(TraceRecordsError) as caught:
            parse_decisions(text)
        assert str(caught.value) == f"{DECISIONS_FILE}: {message}"

    def test_a_record_twice_is_refused(self) -> None:
        once = decisions_text(merge_decisions(no_decisions(), [_record("SR-1", 2, OUT, "reject")]))
        twice = once + once.split("decisions:\n", 1)[1].replace("reject", "accept")
        with pytest.raises(TraceRecordsError) as caught:
            parse_decisions(twice)
        assert caught.value.pointer == "/decisions/1"

    def test_read_from_the_project_directory(self, tmp_path: Path) -> None:
        manifest, _config = write_project(_model(), tmp_path / "project")
        project = require_project(str(manifest))
        assert read_decisions(project) == no_decisions()
        text = decisions_text(merge_decisions(no_decisions(), [_record("SR-1", 2, OUT, "reject")]))
        (tmp_path / "project" / DECISIONS_FILE).write_text(text, encoding="utf-8", newline="\n")
        assert rejected_pairs(read_decisions(project)) == frozenset({("SR-1", 2, OUT)})
        (tmp_path / "project" / DECISIONS_FILE).write_text("formatVersion: 3\n", encoding="utf-8", newline="\n")
        with pytest.raises(TraceRecordsError, match=r"^trace-decisions\.yaml: /formatVersion: must be 1$"):
            read_decisions(project)


def test_the_decisions_bytes_are_the_ones_read_and_a_non_utf8_file_is_refused(tmp_path: Path) -> None:
    manifest, _config = write_project(_model(), tmp_path / "project")
    project = require_project(str(manifest))
    path = tmp_path / "project" / DECISIONS_FILE
    assert read_decisions_bytes(project) == (path, None, no_decisions())
    text = decisions_text(merge_decisions(no_decisions(), [_record("SR-1", 2, OUT, "reject")]))
    path.write_text(text, encoding="utf-8", newline="\n")
    location, data, decisions = read_decisions_bytes(project)
    assert (location, data) == (path, text.encode("utf-8"))
    assert decisions == read_decisions(project)
    path.write_bytes(b"\xff")
    for reader in (read_decisions_bytes, read_decisions):
        with pytest.raises(
            TraceRecordsError, match=r"^trace-decisions\.yaml: \(top level\): cannot be read \(it is not UTF-8 text\)$"
        ):
            reader(project)

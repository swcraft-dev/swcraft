"""``discover`` inside the response budget, and the tripwires on the contract's wire size.

A discovery is measured by ``read.measure`` against ``read.RESPONSE_BUDGET_WIRE_BYTES`` and refused rather than
truncated; the refusal carries no address (a discovery is not addressed) and hints narrower discoveries.

The tripwires are assertions on the shipped contract's own size, measured on ``examples/basic`` through the same
``read.measure`` the engine refuses with, so the tripwire and the enforcement cannot disagree. The response is a
function of the contract rather than of the model, so one project measures it:

* the ``summary`` default stays under the budget by the margin :data:`SUMMARY_MARGIN`;
* the project-wide ``full`` stays over the budget and is refused, and every layer-narrowed ``full`` fits with no
  margin, so the refusal's chain (``summary``, or one layer at a time) is walkable;
* the MCP host's default ``baseline`` (the passport, the token and the ``reads`` block, no layer descriptor) stays
  under :data:`BASELINE_CEILING_WIRE_BYTES`. The contract the host receives travels in the profile's generated
  schema section, whose tripwire is ``test_agent_schema_drift.py``.

When a tripwire fires the answer is a decision (narrow what a descriptor publishes, or move the bound deliberately
with the measurement recorded), never a number to update. The ``RECORDED_*`` constants are a record, not a bound:
:func:`test_the_recorded_figures_are_the_live_measurement` holds them to the live measurement, so a contract change
that moves a wire response fails there with the new figures.

What this does not reach: one project on one contract is measured, and nothing here observes a host that frames or
duplicates the response differently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
from swcraft_tools.editing import read
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.discovery import discover, narrower_discovers
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.project import require_project

#: The margin the ``summary`` default must keep under the budget, as a fraction
#: of the budget: the tripwire is ``budget × (1 − SUMMARY_MARGIN)``, so raising
#: the allowed summary means lowering this constant.
#:
#: When the tripwire fires the answer is a decision, never an edit that turns a
#: red green: narrow what a descriptor publishes, or move the margin
#: deliberately, to the largest multiple of ``0.01`` that leaves at least one
#: median kind's price of headroom, with the measurement recorded. Dropping a
#: FACT from a descriptor stays rejected: an identity, a discriminator, a member
#: or a reference fact, or a compact field string in place of the descriptor.
#: Omitting a declared default drops none (absence means ``false``), which the
#: restoration half of the oracle in ``test_editing_discovery.py`` shows.
SUMMARY_MARGIN = 0.47

#: The live engine's measurement on the seed every test here reads, as a RECORD
#: and not a bound. :func:`test_the_recorded_figures_are_the_live_measurement`
#: holds them equal to the measurement, so a contract change that moves a wire
#: response turns the record red with the new figures in the message (a widened ``enum`` too,
#: as the rated ``asil`` descriptors' decomposed spellings of
#: ``[TPS_SAFEX_00201]`` did). Re-measuring is the whole answer to that red; the
#: margin decision is :data:`SUMMARY_MARGIN`'s, taken only when its own tripwire
#: fires.
RECORDED_SUMMARY_WIRE_BYTES = 42_064
RECORDED_PROJECT_FULL_WIRE_BYTES = 185_986
#: The per-layer ``full`` figures, the WIDEST first. The
#: first key is asserted to be the widest of every declared layer.
RECORDED_LAYER_FULL_WIRE_BYTES = {
    "swc": 57_817,
    "implementation_types": 22_694,
    "application_types": 23_289,
    "compositions": 13_418,
    "hardware": 16_769,
    "compu_methods": 11_468,
    "data_constraints": 5_023,
    "units": 4_537,
}

#: The project-wide ``discover`` the MCP host is served when it names no form
#: (``handle_request(discover_default="baseline")``, the server's one dispatch
#: site), in the unit :func:`read.measure` counts — the escaped one-block copy.
#: It equals the host's text block because
#: ``packages/mcp/tests/test_response_budget.py`` holds :func:`read.measure` to
#: the live block. The answer is the passport, the token and the ``reads`` block,
#: and no layer descriptor: on a fixed project the ``reads`` block moves it, and so
#: does the length of a passport spelling (the project name, ``arxml_version``, the
#: SWCraft version).
RECORDED_BASELINE_WIRE_BYTES = 2_464
#: The ceiling the host's ``baseline`` is asserted under: 4 096 bytes, 5.12 % of
#: the 80 000-byte budget, leaving 4 096 − 2 464 = 1 632 bytes of headroom over
#: :data:`RECORDED_BASELINE_WIRE_BYTES`, about two and a half times the largest
#: single ``reads`` addition recorded (+635). A ``reads`` addition that crossed it would
#: make the host's first call grow toward the ``summary`` the ``baseline``
#: default avoids: a decision, not a number to update.
BASELINE_CEILING_WIRE_BYTES = 4_096


@pytest.fixture
def project(tmp_path: Path) -> Any:
    """``examples/basic``, through the vector seeds' own copy of it."""
    root = fixtures.build_fixture(tmp_path / "budget", "discover-project-all-layers-present")
    return require_project(str(root / "swcraft.yaml"))


def _project_of(root: Any) -> Any:
    """A built fixture's root as the project object ``discover`` takes."""
    return require_project(str(Path(root) / "swcraft.yaml"))


def _wire(project: Any, **kwargs: Any) -> int:
    """The wire size of a discovery **whether or not it is admitted**.

    Over the budget it is read from the refusal's ``Measured.wire_bytes`` (the
    same :func:`read.measure`), so the tripwires stay falsifiable.
    """
    try:
        return read.measure(discover(project, **kwargs))
    except read.OverBudget as refused:
        return refused.measured.wire_bytes


# The tripwires


def test_the_default_discover_keeps_its_margin_under_the_budget(project: Any) -> None:
    budget = read.RESPONSE_BUDGET_WIRE_BYTES
    size = _wire(project)
    assert size <= budget * (1 - SUMMARY_MARGIN), (
        f"the DEFAULT `discover` measures {size} wire bytes, above the "
        f"{SUMMARY_MARGIN:.0%} margin this phase reserved under the {budget}-byte budget "
        f"({budget * (1 - SUMMARY_MARGIN):.0f} bytes). The contract grew and the response every MCP client "
        "gets grew with it. The margin exists so the summary form has room, and "
        "shrinking it is a decision, not a fix — re-measure, and either narrow what a descriptor "
        "publishes without dropping a fact or move the margin deliberately, with the record SUMMARY_MARGIN's "
        "comment asks for."
    )


def test_the_hosts_baseline_discover_stays_under_its_ceiling(tmp_path: Path) -> None:
    """What the MCP host is served when it names no form stays under its ceiling.

    What it does not reach: the host's framing outside the text block, and any
    project other than ``examples/basic``.
    """
    root = fixtures.build_fixture(tmp_path / "baseline", "discover-project-all-layers-present")
    answer = handle_request({"operation": "discover"}, project=root, discover_default="baseline")
    assert answer["form"] == "baseline"
    size = read.measure(answer)
    assert size <= BASELINE_CEILING_WIRE_BYTES, (
        f"the host's default `discover` (`baseline`) measures {size} wire bytes, above its "
        f"{BASELINE_CEILING_WIRE_BYTES}-byte ceiling. The `reads` block grew: every first call the host makes "
        "carries it. Narrow what the block publishes or move the ceiling deliberately, with the arithmetic "
        "beside BASELINE_CEILING_WIRE_BYTES — a decision, not a number to update."
    )
    # No equality with RECORDED_BASELINE_WIRE_BYTES: the ceiling is the guard, and
    # a `reads` change well under it, or a longer `swcraft_version`, is not a decision.
    assert RECORDED_BASELINE_WIRE_BYTES <= BASELINE_CEILING_WIRE_BYTES


def test_the_project_wide_full_discover_is_refused_over_the_budget(project: Any) -> None:
    """The inverted tripwire: the project-wide ``full`` is over the budget and refused.

    A ``full`` that fit again would change what every client is told (the refusal
    chain, the per-layer vectors, the documents), so it fails rather than passes.
    """
    budget = read.RESPONSE_BUDGET_WIRE_BYTES
    size = _wire(project, form="full")
    assert size > budget, (
        f'`discover` at `form: "full"` measures {size} wire bytes, within the server\'s {budget}-byte '
        "budget, so the project-wide `full` is ANSWERED again. The contract shrank, or the descriptor "
        "prose did. `negative/discover-project-full-over-budget.json` "
        "records the refusal, the ten `examples/discover-layer-<layer>-full.json` vectors carry the prose "
        "one layer at a time — every one of them is a decision to revisit, not a number to update."
    )
    with pytest.raises(read.OverBudget):
        discover(project, form="full")


def test_every_layer_narrowed_full_discover_fits_the_budget_with_no_margin(project: Any) -> None:
    """Every layer-narrowed ``full`` fits with no margin, so the refusal chain is walkable.

    A layer that crossed the bound would need a third step in the chain.
    """
    budget = read.RESPONSE_BUDGET_WIRE_BYTES
    for layer in reg.current().layer_names:
        size = read.measure(discover(project, layer=layer, form="full"))
        assert size <= budget, (
            f'`discover` at `layer: "{layer}", form: "full"` measures {size} wire bytes, above the '
            f"{budget}-byte budget — the refusal chain of a project-wide `full` now dead-ends at this layer. "
            "A per-layer `full` was the answer the regime kept; it needs a narrower step now."
        )


def test_the_full_form_has_no_margin_and_the_asymmetry_is_deliberate(project: Any) -> None:
    """``full`` is over the summary's tripwire: the two bounds are not one assertion twice.

    Trivially true while ``full`` is refused; kept for the day it fits again.
    """
    budget = read.RESPONSE_BUDGET_WIRE_BYTES
    assert _wire(project, form="full") > budget * (1 - SUMMARY_MARGIN)


def test_the_prose_is_the_majority_of_the_full_answer(project: Any) -> None:
    """The prose is the majority of ``full``, which is why ``summary`` is the default."""
    full = _wire(project, form="full")
    summary = _wire(project)
    assert summary < full / 2, f"summary {summary}, full {full}"


def test_the_recorded_figures_are_the_live_measurement(project: Any) -> None:
    """The record is the current measurement, and its first layer is the widest.

    A figure that moves is a record to re-measure; one that crosses a bound is
    the tripwires' question.

    What it does not reach: any project other than ``examples/basic``, and any
    host framing (the ~70 bytes :func:`read.measure` leaves out).
    """
    layer_full = {layer: _wire(project, layer=layer, form="full") for layer in reg.current().layer_names}
    measured = {
        "summary": _wire(project),
        "project-wide full": _wire(project, form="full"),
        **{f"{layer} full": layer_full[layer] for layer in RECORDED_LAYER_FULL_WIRE_BYTES},
    }
    recorded = {
        "summary": RECORDED_SUMMARY_WIRE_BYTES,
        "project-wide full": RECORDED_PROJECT_FULL_WIRE_BYTES,
        **{f"{layer} full": size for layer, size in RECORDED_LAYER_FULL_WIRE_BYTES.items()},
    }
    assert measured == recorded, (
        f"the `discover` record is stale: measured {measured}, recorded {recorded}. A contract change moved a "
        "wire response. Re-measure the RECORDED_* constants. Whether "
        "the margin must move is test_the_default_discover_keeps_its_margin_under_the_budget's question, not "
        "this test's."
    )
    widest = max(layer_full, key=layer_full.__getitem__)
    assert widest == next(iter(RECORDED_LAYER_FULL_WIRE_BYTES)), (
        f"the widest per-layer `full` is now `{widest}` at {layer_full[widest]} wire bytes, and the record "
        "still names another layer. Re-measure it."
    )


# The regime: measured, refused, addressless


def test_a_discovery_above_the_budget_is_refused_rather_than_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fixtures.build_fixture(tmp_path / "refused", "discover-project-all-layers-present")
    monkeypatch.setattr(read, "RESPONSE_BUDGET_WIRE_BYTES", 1_000)
    refused = handle_request({"operation": "discover"}, project=root)
    assert refused["response"] == "error"
    assert refused["error"] == "over-budget"
    assert refused["failure_class"] == "invalid"


def test_the_addressless_refusal_grows_by_the_reads_block_and_by_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal carries no ``reads`` block, yet its wire figure counts ``reads.derived``.

    ``discovery.py`` builds the answer with ``reads_capability()`` before
    measuring it, so withholding the vocabulary changes only the wire figure, by
    the entries' cost: 1 876 bytes, moved by any ``reads.derived`` entry added or
    changed (the key is required, so the nearest server publishing none keeps an
    empty array).
    """
    root = fixtures.build_fixture(tmp_path / "refused", "discover-project-all-layers-present")
    with_vocabulary = handle_request({"operation": "discover", "form": "full"}, project=root)
    published = read.reads_capability

    def without_vocabulary(discover_default: Any = None) -> Any:
        from dataclasses import replace

        return replace(published(discover_default), derived=())

    monkeypatch.setattr(read, "reads_capability", without_vocabulary)
    withheld = handle_request({"operation": "discover", "form": "full"}, project=root)
    assert with_vocabulary["error"] == withheld["error"] == "over-budget"
    assert with_vocabulary["hints"] == withheld["hints"]
    assert with_vocabulary["measured"]["elements"] == withheld["measured"]["elements"]
    delta = read.measure(published().to_json()) - read.measure(without_vocabulary().to_json())
    assert delta == 1_876
    assert with_vocabulary["measured"]["wire_bytes"] - withheld["measured"]["wire_bytes"] == delta


def test_the_refusal_carries_no_address_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A discovery is not addressed, so its refusal omits ``addresses``.

    Never ``[]`` (refused by ``$defs/errorDetail``'s ``minItems``) and never an
    invented address.
    """
    root = fixtures.build_fixture(tmp_path / "addressless", "discover-project-all-layers-present")
    monkeypatch.setattr(read, "RESPONSE_BUDGET_WIRE_BYTES", 1_000)
    refused = handle_request({"operation": "discover"}, project=root)
    assert "addresses" not in refused
    assert "at {" not in refused["message"], refused["message"]


def test_the_measured_block_counts_the_layer_descriptors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``measured.elements`` counts the refused response's layer descriptors, never the model."""
    root = fixtures.build_fixture(tmp_path / "measured", "discover-project-all-layers-present")
    monkeypatch.setattr(read, "RESPONSE_BUDGET_WIRE_BYTES", 1_000)
    declared = len(reg.current().layers)
    refused = handle_request({"operation": "discover"}, project=root)
    assert refused["measured"] == {
        "wire_bytes": refused["measured"]["wire_bytes"],
        "budget": 1_000,
        "elements": declared,
    }
    assert refused["measured"]["wire_bytes"] > 1_000
    assert f"{declared} element(s)" in refused["message"]

    narrowed = handle_request({"operation": "discover", "layer": "swc"}, project=root)
    assert narrowed["measured"]["elements"] == 1, "a narrowed refusal would have carried one descriptor"


def test_a_refused_full_hints_the_summary_and_then_every_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = fixtures.build_fixture(tmp_path / "chain", "discover-project-all-layers-present")
    monkeypatch.setattr(read, "RESPONSE_BUDGET_WIRE_BYTES", 1_000)
    refused = handle_request({"operation": "discover", "form": "full"}, project=root)
    assert refused["hints"] == [{"operation": "discover", "form": "summary"}] + [
        {"operation": "discover", "layer": name, "form": "full"} for name in reg.current().layer_names
    ]


def test_a_refused_summary_hints_the_layers_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = fixtures.build_fixture(tmp_path / "chain2", "discover-project-all-layers-present")
    monkeypatch.setattr(read, "RESPONSE_BUDGET_WIRE_BYTES", 1_000)
    refused = handle_request({"operation": "discover"}, project=root)
    assert refused["hints"] == [
        {"operation": "discover", "layer": name, "form": "summary"} for name in reg.current().layer_names
    ]


def test_a_layer_narrowed_summary_ends_the_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The terminating condition: no ``hints`` key at all, never an empty array."""
    root = fixtures.build_fixture(tmp_path / "chain3", "discover-project-all-layers-present")
    monkeypatch.setattr(read, "RESPONSE_BUDGET_WIRE_BYTES", 200)
    refused = handle_request({"operation": "discover", "layer": "swc"}, project=root)
    assert refused["error"] == "over-budget"
    assert "hints" not in refused


def test_a_layer_narrowed_full_offers_that_layers_summary_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fixtures.build_fixture(tmp_path / "chain4", "discover-project-all-layers-present")
    monkeypatch.setattr(read, "RESPONSE_BUDGET_WIRE_BYTES", 200)
    refused = handle_request({"operation": "discover", "layer": "swc", "form": "full"}, project=root)
    assert refused["hints"] == [{"operation": "discover", "layer": "swc", "form": "summary"}]


def test_every_hint_is_a_request_the_server_would_take(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each hint of both refusal chains is a request the server answers.

    Both are walked because they differ: ``full``'s starts with the ``summary``
    step, which a walk of the default chain alone never replays.
    """
    root = fixtures.build_fixture(tmp_path / "walk", "discover-project-all-layers-present")
    # One budget per case, one byte under the answer it must refuse: `full`'s first
    # step IS the whole summary, so no single budget walks both chains.
    summary_bytes = _wire(_project_of(root))
    full_bytes = _wire(_project_of(root), form="full")
    cases = (
        ({"operation": "discover"}, summary_bytes - 1),
        ({"operation": "discover", "form": "full"}, full_bytes - 1),
    )
    for request, budget in cases:
        monkeypatch.setattr(read, "RESPONSE_BUDGET_WIRE_BYTES", budget)
        refused = handle_request(request, project=root)
        assert refused["error"] == "over-budget", (request, budget)
        assert refused["hints"], f"non-vacuous: {request}"
        for hint in refused["hints"]:
            replayed = handle_request(hint, project=root)
            assert replayed["response"] == "discover", (request, hint, replayed)

    # The step only the `full` refusal offers: the same scope at `summary`.
    monkeypatch.setattr(read, "RESPONSE_BUDGET_WIRE_BYTES", full_bytes - 1)
    full_refusal = handle_request({"operation": "discover", "form": "full"}, project=root)
    assert full_refusal["hints"][0] == {"operation": "discover", "form": "summary"}
    assert handle_request(full_refusal["hints"][0], project=root)["form"] == "summary"


def test_the_chain_is_composed_from_the_registry_and_not_from_a_literal() -> None:
    """``narrower_discovers`` at its four inputs, as one table.

    A step offered twice or skipped is a failing row here.
    """
    registry = reg.current()
    names = list(registry.layer_names)
    # Non-vacuity: on an empty registry two of the four rows would be `[] == []`.
    assert len(names) > 1, names
    assert [hint.to_json() for hint in narrower_discovers(None, "full", registry)] == [
        {"operation": "discover", "form": "summary"}
    ] + [{"operation": "discover", "layer": name, "form": "full"} for name in names]
    assert [hint.to_json() for hint in narrower_discovers(None, "summary", registry)] == [
        {"operation": "discover", "layer": name, "form": "summary"} for name in names
    ]
    assert [hint.to_json() for hint in narrower_discovers("swc", "full", registry)] == [
        {"operation": "discover", "layer": "swc", "form": "summary"}
    ]
    assert narrower_discovers("swc", "summary", registry) == ()


def test_an_answered_discovery_is_never_above_the_budget(project: Any) -> None:
    """The bound holds on what is SENT, at both forms and every scope.

    ``discover`` is called directly, not through :func:`_wire`, so a refusal
    fails as an unraised ``OverBudget``. The project-wide ``full`` is the one
    shape refused by design, asserted as a refusal so a second cannot join it.
    """
    budget = read.RESPONSE_BUDGET_WIRE_BYTES
    assert read.measure(discover(project, form="summary")) <= budget
    with pytest.raises(read.OverBudget):
        discover(project, form="full")
    for form in ("summary", "full"):
        for layer in reg.current().layer_names:
            assert read.measure(discover(project, layer=layer, form=form)) <= budget, (layer, form)


def test_a_discovery_exactly_at_the_budget_is_admitted(project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The comparison is ``<=`` and not ``<``, at ``discovery._refuse_over_budget``.

    The budget is found by iteration: it is published inside the response
    (``reads.budget_wire_bytes``), so lowering it can change the size compared.
    """
    budget = read.measure(discover(project))
    for _ in range(4):
        monkeypatch.setattr(read, "RESPONSE_BUDGET_WIRE_BYTES", budget)
        size = read.measure(discover(project))
        if size == budget:
            break
        budget = size
    else:  # pragma: no cover - the fixed point is reached on the first or second round
        pytest.fail(f"no budget makes the response exactly its own size (last {budget})")

    assert read.measure(discover(project)) == budget

    monkeypatch.setattr(read, "RESPONSE_BUDGET_WIRE_BYTES", budget - 1)
    with pytest.raises(read.OverBudget) as refused:
        discover(project)
    assert refused.value.measured.budget == budget - 1

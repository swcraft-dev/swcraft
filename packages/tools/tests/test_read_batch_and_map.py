"""``read`` with several addresses, and with none (the model map)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, agent_task_bench_module, copy_specimen
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.protocol import MODEL_MAP_LIST_MAX
from swcraft_tools.editing.read import OverBudget, measure, model_map
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.project import SwcraftProject, require_project

#: The specimen with exactly one interface over :data:`MODEL_MAP_LIST_MAX` and no more than that
#: many components, so the interfaces are counted and the components still listed.
MAP_BOUND = "map-bound"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "basic"
    shutil.copytree(REPO_ROOT / "examples" / "basic", root, ignore=shutil.ignore_patterns("dist", "views"))
    return root


def _map(loaded: SwcraftProject, **budget: int) -> dict[str, Any]:
    """:func:`~swcraft_tools.editing.read.model_map` as the engine calls it, *budget* forced where named."""
    return model_map(loaded, baseline_token(Path(loaded.resolved_model_root)), reg.or_current(None), **budget)


def _index_identities(project: Path, layer: str) -> list:
    answer = handle_request({"operation": "read", "address": {"layer": layer}, "form": "index"}, project=project)
    return answer["value"]["identities"]


def test_the_map_counts_every_layer_lists_the_small_ones_and_carries_the_baseline(project: Path) -> None:
    answer = handle_request({"operation": "read"}, project=project)
    assert answer["response"] == "map"
    layer_names = list(reg.or_current(None).layer_names)
    assert list(answer["counts"]) == layer_names
    # The basic example has no layer over the bound: every layer is listed,
    # each exactly as its `index` read answers it, in that read's order.
    assert list(answer["layers"]) == layer_names
    for layer in layer_names:
        assert answer["layers"][layer] == _index_identities(project, layer)
        assert answer["counts"][layer] == len(answer["layers"][layer])
    assert answer["layers"]["swc"] == ["ExampleConsumerSWC", "ExampleSWC"]
    discovered = handle_request({"operation": "discover", "form": "baseline"}, project=project)
    assert answer["baseline"] == discovered["baseline"]


def test_a_layer_over_the_bound_is_counted_and_not_listed(tmp_path: Path) -> None:
    """51 interfaces: one over :data:`MODEL_MAP_LIST_MAX`, so the count is answered and the names are not."""
    assert MODEL_MAP_LIST_MAX == 50
    generated = copy_specimen(MAP_BOUND, tmp_path / MAP_BOUND)
    answer = handle_request({"operation": "read"}, project=generated)
    assert answer["counts"]["interfaces"] == MODEL_MAP_LIST_MAX + 1
    assert "interfaces" not in answer["layers"]
    assert answer["layers"]["swc"] == _index_identities(generated, "swc")
    assert list(answer["counts"]) == list(reg.or_current(None).layer_names)


def test_the_map_of_a_large_model_answers_under_four_kilobytes(tmp_path: Path) -> None:
    """The map of the ``large-bench`` specimen answers in a few kilobytes, not the model's."""
    large = agent_task_bench_module().LARGE_SPECIMEN
    generated = copy_specimen(large, tmp_path / large)
    answer = handle_request({"operation": "read"}, project=generated)
    assert answer["response"] == "map"
    crossing = [layer for layer, count in answer["counts"].items() if count > MODEL_MAP_LIST_MAX]
    assert len(crossing) >= 3, answer["counts"]
    assert not set(crossing) & set(answer["layers"])
    size = measure(answer)
    assert size < 4_096, f"the bounded map measured {size} B on the large model"


def test_over_a_forced_budget_the_largest_listed_layer_is_dropped(project: Path) -> None:
    """The shrink loop: a budget under the unshrunk answer drops the largest listed layer, never a count."""
    loaded = require_project(str(project / "swcraft.yaml"))
    full = _map(loaded)
    largest = max(full["layers"], key=lambda layer: (len(full["layers"][layer]), layer))
    forced = measure(full) - 1
    shrunk = _map(loaded, budget=forced)
    assert largest not in shrunk["layers"]
    assert set(shrunk["layers"]) < set(full["layers"])
    for layer in shrunk["layers"]:
        assert shrunk["layers"][layer] == full["layers"][layer]
    assert shrunk["counts"] == full["counts"]
    assert shrunk["baseline"] == full["baseline"]
    assert measure(shrunk) <= forced


def test_a_budget_under_the_counts_alone_is_the_usual_refusal(project: Path) -> None:
    loaded = require_project(str(project / "swcraft.yaml"))
    full = _map(loaded)
    counts_only = measure({**full, "layers": {}})
    with pytest.raises(OverBudget) as refused:
        _map(loaded, budget=counts_only - 1)
    assert refused.value.measured.budget == counts_only - 1
    assert refused.value.measured.elements == sum(full["counts"].values())
    assert len(refused.value.hints) == len(full["counts"])


def test_the_map_refuses_any_other_key(project: Path) -> None:
    assert handle_request({"operation": "read", "form": "index"}, project=project)["error"] == "schema-violation"


def test_a_batch_answers_each_address_as_a_single_read_in_order(project: Path) -> None:
    addresses = [
        {"layer": "swc", "element": "ExampleSWC"},
        {"layer": "swc", "element": "NotThere"},
        {"layer": "tasks", "element": "Task_10ms"},
    ]
    answer = handle_request({"operation": "read", "addresses": addresses, "form": "summary"}, project=project)
    assert answer["response"] == "reads"
    first, missing, task = answer["answers"]
    assert first == handle_request({"operation": "read", "address": addresses[0], "form": "summary"}, project=project)
    assert missing["error"] == "broken-reference"
    assert task["address"] == addresses[2]
    assert measure(answer) > measure(first)


@pytest.mark.parametrize(
    "request_body",
    [
        {"operation": "read", "addresses": []},
        {"operation": "read", "addresses": [{"layer": "swc"}] * 17},
        {"operation": "read", "addresses": [{"layer": "swc"}], "where": [{"field": "name", "equals": "x"}]},
        {
            "operation": "read",
            "address": {"layer": "swc", "element": "ExampleSWC"},
            "addresses": [{"layer": "swc", "element": "ExampleSWC"}],
        },
    ],
    ids=["empty", "seventeen", "where", "address-and-addresses"],
)
def test_a_malformed_batch_is_a_schema_violation(project: Path, request_body: dict) -> None:
    assert handle_request(request_body, project=project)["error"] == "schema-violation"


@pytest.mark.parametrize("count", [1, 2, 16])
def test_a_batch_within_the_bound_answers_every_address(project: Path, count: int) -> None:
    """The bound is 1 to 16 inclusive: its two ends and a batch of two are answered, one answer per address."""
    address = {"layer": "swc", "element": "ExampleSWC"}
    answer = handle_request({"operation": "read", "addresses": [address] * count, "form": "summary"}, project=project)
    assert answer["response"] == "reads"
    assert len(answer["answers"]) == count


def test_a_batch_naming_no_form_answers_each_address_in_its_own_default_form(project: Path) -> None:
    addresses = [{"layer": "swc", "element": "ExampleSWC"}, {"layer": "swc"}]
    answer = handle_request({"operation": "read", "addresses": addresses}, project=project)
    for address, single in zip(addresses, answer["answers"], strict=True):
        assert single == handle_request({"operation": "read", "address": address}, project=project)


def test_a_batch_over_the_budget_is_refused_with_the_single_reads_as_hints(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import swcraft_tools.editing.read as read_module

    addresses = [{"layer": "swc", "element": "ExampleSWC"}, {"layer": "swc", "element": "ExampleConsumerSWC"}]
    # The batch's budget is the single reads' (`read.read_many`), so lowering it
    # refuses each address too; the batch is refused over the whole answer.
    monkeypatch.setattr(read_module, "RESPONSE_BUDGET_WIRE_BYTES", 64)
    refusal = handle_request({"operation": "read", "addresses": addresses, "form": "summary"}, project=project)
    assert refusal["error"] == "over-budget"
    assert refusal["measured"]["budget"] == 64
    assert refusal["hints"] == [{"operation": "read", "address": address, "form": "summary"} for address in addresses]


def test_a_tie_between_the_largest_listed_layers_drops_the_later_name(project: Path) -> None:
    loaded = require_project(str(project / "swcraft.yaml"))
    full = _map(loaded)
    largest = max(len(names) for names in full["layers"].values())
    tied = sorted(layer for layer, names in full["layers"].items() if len(names) == largest)
    assert len(tied) >= 2, tied  # the basic example's base and implementation types, twelve each
    shrunk = _map(loaded, budget=measure(full) - 1)
    assert set(full["layers"]) - set(shrunk["layers"]) == {tied[-1]}


def test_a_counts_only_refusal_pages_the_hint_of_a_layer_over_the_bound(tmp_path: Path) -> None:
    generated = copy_specimen(MAP_BOUND, tmp_path / MAP_BOUND)
    loaded = require_project(str(generated / "swcraft.yaml"))
    with pytest.raises(OverBudget) as refused:
        _map(loaded, budget=1)
    hints = {hint.address.layer: hint.to_json() for hint in refused.value.hints}
    assert hints["interfaces"] == {
        "operation": "read",
        "address": {"layer": "interfaces"},
        "form": "index",
        "limit": 50,
    }
    assert "limit" not in hints["swc"]
    for hint in hints.values():
        assert "error" not in handle_request(hint, project=generated), hint


def test_an_unparseable_element_file_refuses_the_map_without_a_path(project: Path) -> None:
    (project / "model" / "swc" / "example" / "Broken.yaml").write_text(
        "name: [unclosed\n", encoding="utf-8", newline="\n"
    )
    refusal = handle_request({"operation": "read"}, project=project)
    assert "error" in refusal
    assert str(project) not in str(refusal)
    assert any(address.get("element") == "Broken" for address in refusal.get("addresses", []))


def test_both_new_answers_parse_as_protocol_responses(project: Path) -> None:
    from swcraft_tools.editing.protocol import ModelMapResponse, ReadManyResponse, parse_response

    reads = handle_request(
        {
            "operation": "read",
            "addresses": [{"layer": "swc", "element": "ExampleSWC"}, {"layer": "swc", "element": "No"}],
        },
        project=project,
    )
    parsed = parse_response(reads)
    assert isinstance(parsed, ReadManyResponse)
    assert parsed.to_json() == reads
    mapped = handle_request({"operation": "read"}, project=project)
    parsed_map = parse_response(mapped)
    assert isinstance(parsed_map, ModelMapResponse)
    assert parsed_map.to_json() == mapped

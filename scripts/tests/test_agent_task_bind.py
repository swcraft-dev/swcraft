"""``scripts/agent_task_bind.py`` binds the any-model set to a model, and its answers are the CLI's.

The binder is held against oracles that are not the binder: on the showcase and on a passport
example's specimen, each question's expectation equals the CLI's recomputation for the bound
target, in the CLI's row order; task 21's phrase rule, which no query answers, is recomputed here;
and where the binding picks the committed showcase task's target, the expectation is the committed
one.

Each change template's right run passes the checker on a scratch copy and the untouched copy fails
it, on both models; on the showcase the rendered assertions are the committed task's and the right
run moves what the committed expected state moves. Benchmark task 10's export is the product's own and reads
back through ``swcraft import component``.

Not reached: the large bench's bindings (``test_agent_task_bench_large.py``), an imported model's
shape, and anything an agent does with a rendered prompt; 03 and 14 are held to the CLI on the
showcase alone (the specimen states no mapping set and no declared level), 17's tie only on a
stubbed memory report, and 36 on the showcase alone (no specimen's runnable calls an operation).
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    AGENT_TASK_BENCH,
    REPO_ROOT,
    agent_task_bench_module,
    agent_task_bind_module,
    agent_task_check_module,
    copy_specimen,
)
from swcraft_tools import requirements_import
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import stored_element_files
from swcraft_tools.schema import Composition, DataConstr, ExternalDataType, Linear, Partition, PortComSpec, Unit
from swcraft_tools.yaml_load import load_yaml_document

BIND = agent_task_bind_module()
CHECK = agent_task_check_module()
SET_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "agent-bench"
TEMPLATES_DIR = SET_DIR / "templates"

#: The specimen the generated tier is bound on (the passport example of that name): hubs of 120
#: and 100 ports, an interface 216 ports name and twelve unconnected ports, so every list of 08, 11
#: and 12 is a long one.
SPECIMEN_EXAMPLE = "example-configuration-only-controlled"
#: The smallest example's specimen: one connector between its two components, no unconnected port.
MINIMAL_EXAMPLE = "example-minimal"

#: The models a question's expectation is held to the CLI's on, by fixture name.
MODELS = ("showcase", "specimen")

#: What each model leaves not applicable, by template id: the specimen states no mapping set and no
#: declared level, its most linked requirement has more than twenty link sites, and none of its
#: runnables calls an operation.
NOT_APPLICABLE_ON = {
    "showcase": {},
    "specimen": {
        "03-new-interface-and-type": "no-mapping-set",
        "14-asil-b-count": "no-rated-component",
        "18-elements-satisfying-a-requirement": "long-site-list",
        "36-call-asynchronously": "no-synchronous-call",
    },
}


def _applicable(template_id: str) -> tuple[str, ...]:
    """The models of :data:`MODELS` that bind *template_id*."""
    return tuple(name for name in MODELS if template_id not in NOT_APPLICABLE_ON[name])


#: The change templates that add, and those that move, remove or rename.
ADDING = (
    "01-connect-two-components",
    "03-new-interface-and-type",
    "07-add-requirement",
    "10-import-component",
    "23-link-a-port-to-a-requirement",
    "36-call-asynchronously",
)
MOVING = (
    "04-move-runnable",
    "05-remove-component",
    "06-rename-data-element",
    "24-confirm-links-at-a-new-version",
    "35-make-a-read-implicit",
)
#: Every change template, each one rendered on both models.
CHANGES = tuple(sorted(ADDING + MOVING, key=str))

#: Every change template, on a model whose ``check-model`` reports an error.
CHANGES_ON_AN_ERROR = dict.fromkeys(CHANGES, "baseline-has-errors")

#: The product-rule questions' codes on a model with no hardware budget, no rating and no requirement.
PRODUCT_RULE_NOT_APPLICABLE = {
    "09-ram-for-a-component": "no-core-with-one-ram",
    "14-asil-b-count": "no-rated-component",
    "17-largest-ram-consumer": "no-memory-consumer",
    "19-unallocated-requirements": "no-unallocated-requirement",
    "20-links-needing-confirmation": "no-stale-link",
    "22-candidates-for-a-requirement": "fewer-than-three-candidates",
}


def _specimen(root: Path, name: str) -> Path:
    """A copy of the specimen *name* under *root*, with the large bench's manifest; its manifest returned."""
    manifest = copy_specimen(name, root / name) / "swcraft.yaml"
    manifest.write_text(agent_task_bench_module().MANIFEST, encoding="utf-8", newline="\n")
    return manifest


@pytest.fixture(scope="session")
def showcase() -> Any:
    return BIND.BoundModel.load(AGENT_TASK_BENCH / "swcraft.yaml")


@pytest.fixture(scope="session")
def specimen(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return BIND.BoundModel.load(_specimen(tmp_path_factory.mktemp("bind-specimen"), SPECIMEN_EXAMPLE))


@pytest.fixture(scope="session")
def minimal(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return BIND.BoundModel.load(_specimen(tmp_path_factory.mktemp("bind-minimal"), MINIMAL_EXAMPLE))


@pytest.fixture(scope="session")
def bare(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """The minimal specimen with its requirements and its partitions removed: no generated model has neither."""
    manifest = _specimen(tmp_path_factory.mktemp("bind-bare"), MINIMAL_EXAMPLE)
    model_root = manifest.parent / "model"
    registry = current_registry()
    loaded = BIND.BoundModel.load(manifest)
    partitions = {name for name, element in loaded.by_name("hardware").items() if isinstance(element, Partition)}
    assert partitions and loaded.names("requirements")
    removed = [
        *(files for files in stored_element_files(model_root, "requirements", registry=registry).values()),
        *(
            files
            for name, files in stored_element_files(model_root, "hardware", registry=registry).items()
            if name in partitions
        ),
    ]
    for files in removed:
        for layer_file in files:
            layer_file.path.unlink()
    return BIND.BoundModel.load(manifest)


@pytest.fixture(scope="session")
def bound(showcase: Any, specimen: Any) -> dict[str, dict[str, Any]]:
    """The bind result per model, computed once per session."""
    return {"showcase": BIND.bind_all(showcase), "specimen": BIND.bind_all(specimen)}


def _templates() -> dict[str, dict[str, Any]]:
    return {template["id"]: template for template in BIND.load_templates()}


def _committed(task_id: str) -> dict[str, Any]:
    return json.loads((SET_DIR / "tasks" / f"{task_id}.json").read_text(encoding="utf-8"))


def _cli(model: Any, *arguments: str) -> Any:
    completed = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", *arguments, "--project", model.manifest.as_posix()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(completed.stdout)


def _query(model: Any, request: dict[str, Any], tmp_path: Path) -> list[dict[str, Any]]:
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(request), encoding="utf-8", newline="\n")
    document = _cli(model, "query", "--request", request_file.as_posix(), "--format", "json")
    assert "next" not in document, "the recomputation reads one page"
    return document.get("elements", document.get("rows"))


def _answer(rendered: Any) -> dict[str, Any]:
    assert isinstance(rendered, BIND.Rendered), rendered
    return rendered.expect["answer"]


def _rendered(results: dict[str, Any]) -> dict[str, Any]:
    """The templates *results* rendered, not-applicable ones left out."""
    return {template_id: result for template_id, result in results.items() if isinstance(result, BIND.Rendered)}


def _questions(results: dict[str, Any]) -> dict[str, Any]:
    """The question templates *results* rendered."""
    return {template_id: result for template_id, result in _rendered(results).items() if "answer" in result.expect}


def _changes(results: dict[str, Any]) -> dict[str, Any]:
    """The change templates *results* rendered."""
    return {template_id: result for template_id, result in _rendered(results).items() if "answer" not in result.expect}


def _right_reply(answer: dict[str, Any]) -> str:
    """A reply an agent that knows the right answer writes: one line per name, a site with its layer."""
    lines = []
    if "first_of" in answer:
        lines.append(f"{answer['first_of']['expected']}.")
    for name in answer.get("candidates", {}).get("top", []):
        lines.append(f"- {name}, sharing {', '.join(answer['candidates']['terms'][name])}")
    if "number" in answer:
        lines.append(f"It has {answer['number']}.")
    names = answer.get("mentions_all", [])
    if names:
        lines.append(f"{len(names)} in all:")
        lines.extend(f"- {name}" for name in names)
    for site in answer.get("with_layer", []):
        word = CHECK.LAYER_WORDS[site["layer"]][0]
        owner = f" of component {site['owner']}" if "owner" in site else ""
        lines.append(f"- {word} {site['name']}{owner}")
    pairs = answer.get("with_owner", [])
    if pairs:
        lines.append(f"{len(pairs)} in all:")
        lines.extend(f"- {pair['owner']}.{pair['name']}" for pair in pairs)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The frame.
# ---------------------------------------------------------------------------


def test_the_template_directory_and_the_bindings_are_one_set() -> None:
    templates = BIND.load_templates()
    names = [template["binding"] for template in templates]
    assert len(names) == len(set(names)), "one binding per template"
    assert set(names) == set(BIND.BINDINGS)
    assert [path.stem for path in sorted(TEMPLATES_DIR.glob("*.json"), key=lambda path: path.as_posix())] == [
        template["id"] for template in templates
    ]


def test_no_template_exists_for_task_02() -> None:
    assert all(template["task"] != 2 for template in BIND.load_templates())
    assert not list(TEMPLATES_DIR.glob("02-*"))


@pytest.mark.parametrize("template_id", sorted(_templates(), key=str))
def test_a_template_keeps_the_committed_tasks_identity(template_id: str) -> None:
    template = _templates()[template_id]
    committed = _committed(template_id)
    assert {key: template[key] for key in ("id", "task", "group", "kind")} == {
        key: committed[key] for key in ("id", "task", "group", "kind")
    }


@pytest.mark.parametrize("name", MODELS)
def test_every_placeholder_of_a_prompt_is_filled(name: str, bound: dict[str, dict[str, Any]]) -> None:
    templates = _templates()
    for template_id, rendered in _rendered(bound[name]).items():
        assert BIND.placeholders_in(templates[template_id]["prompt"]) == set(rendered.values), template_id
        task = BIND.render_task(templates[template_id], rendered)
        assert "{{" not in task["prompt"] and "}}" not in task["prompt"], template_id


def test_a_rendered_task_has_the_committed_formats_keys_and_the_checker_reads_it(
    bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    committed_keys = set().union(*(_committed(template_id).keys() for template_id in _templates()))
    for template_id, rendered in bound["showcase"].items():
        task = BIND.render_task(_templates()[template_id], rendered)
        assert set(task) - {"set", "oracle"} <= committed_keys, template_id
        assert task["set"] == CHECK.ANY_SET and task["oracle"] in BIND.ORACLES
        path = tmp_path / f"{template_id}.json"
        path.write_text(json.dumps(task, indent=2), encoding="utf-8", newline="\n")
        assert CHECK.load_task_file(path) == task


def test_render_task_refuses_values_that_do_not_fill_the_prompt(showcase: Any) -> None:
    template = _templates()["11-ports-of-a-component"]
    rendered = BIND.bind(showcase, template)
    wrong = BIND.Rendered(values={"interface": "X"}, expect=rendered.expect, reference=rendered.reference)
    with pytest.raises(BIND.TemplateError, match="the binding fills"):
        BIND.render_task(template, wrong)


def test_the_binders_constants_are_the_checkers() -> None:
    assert BIND.LONG_LIST_HEAD == CHECK.LONG_LIST_HEAD
    assert BIND.ANY_SET == CHECK.ANY_SET
    assert BIND.KINDS == tuple(kind for kind in CHECK.KINDS if kind != "guide")


def test_a_not_applicable_code_is_one_of_the_closed_set() -> None:
    assert BIND.NotApplicable("no-unconnected-port").code == "no-unconnected-port"
    with pytest.raises(ValueError, match="not a not-applicable code"):
        BIND.NotApplicable("no-such-code")
    assert list(BIND.NOT_APPLICABLE) == sorted(set(BIND.NOT_APPLICABLE), key=str)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"extra": 1}, "unknown keys"),
        ({"oracle": "guess"}, "has oracle"),
        ({"kind": "chat"}, "has kind"),
        ({"binding": "no_such_binding"}, "names no binding"),
        ({"id": "11-another"}, "carries the id"),
    ],
)
def test_a_malformed_template_is_refused(change: dict[str, Any], message: str, tmp_path: Path) -> None:
    template = {**_templates()["11-ports-of-a-component"], **change}
    (tmp_path / "11-ports-of-a-component.json").write_text(json.dumps(template), encoding="utf-8", newline="\n")
    with pytest.raises(BIND.TemplateError, match=message):
        BIND.load_templates(tmp_path)


def test_a_template_missing_a_key_or_unreadable_is_refused(tmp_path: Path) -> None:
    template = dict(_templates()["11-ports-of-a-component"])
    del template["prompt"]
    (tmp_path / "11-ports-of-a-component.json").write_text(json.dumps(template), encoding="utf-8", newline="\n")
    with pytest.raises(BIND.TemplateError, match="missing keys"):
        BIND.load_templates(tmp_path)
    (tmp_path / "11-ports-of-a-component.json").write_text("{", encoding="utf-8", newline="\n")
    with pytest.raises(BIND.TemplateError, match="cannot be read: JSONDecodeError"):
        BIND.load_templates(tmp_path)


def test_a_bound_model_and_a_rendering_name_nothing_when_printed(
    showcase: Any, bound: dict[str, dict[str, Any]]
) -> None:
    assert repr(showcase) == "BoundModel()"
    assert {repr(rendered) for rendered in bound["showcase"].values()} == {"Rendered()"}


# ---------------------------------------------------------------------------
# The bindings, held to the CLI and to the committed showcase tasks.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", MODELS)
def test_each_template_renders_or_is_not_applicable_as_expected(name: str, bound: dict[str, dict[str, Any]]) -> None:
    assert set(bound[name]) == set(_templates())
    assert {
        template_id: result.code
        for template_id, result in bound[name].items()
        if isinstance(result, BIND.NotApplicable)
    } == NOT_APPLICABLE_ON[name]


def oracle_08_the_direct_users_are_impacts_first_level_in_its_order(model: Any, rendered: Any, tmp_path: Path) -> None:
    document = _cli(model, "impact", rendered.values["interface"], "--depth", "1", "--format", "json")
    assert [entry["name"] for entry in document["matched"]["interface"]] == _answer(rendered)["mentions_all"]


@pytest.mark.parametrize("name", MODELS)
def test_08_the_direct_users_are_impacts_first_level_in_its_order(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    oracle_08_the_direct_users_are_impacts_first_level_in_its_order(
        request.getfixturevalue(name), bound[name]["08-interface-change-impact"], tmp_path
    )


def oracle_11_the_ports_are_the_members_read_in_port_order(model: Any, rendered: Any, tmp_path: Path) -> None:
    rows = _query(model, {"address": {"layer": "swc"}, "members": "ports"}, tmp_path)
    ports = [row["member"] for row in rows if row["element"] == rendered.values["component"]]
    assert ports == _answer(rendered)["mentions_all"]
    assert len(ports) == _answer(rendered)["number"]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["element"]] = counts.get(row["element"], 0) + 1
    assert counts[rendered.values["component"]] == max(counts.values())


@pytest.mark.parametrize("name", MODELS)
def test_11_the_ports_are_the_members_read_in_port_order(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    oracle_11_the_ports_are_the_members_read_in_port_order(
        request.getfixturevalue(name), bound[name]["11-ports-of-a-component"], tmp_path
    )


def oracle_12_the_components_are_the_where_on_the_interface_in_row_order(
    model: Any, rendered: Any, tmp_path: Path
) -> None:
    request_body = {
        "address": {"layer": "swc"},
        "members": "ports",
        "where": [{"field": "interface", "equals": rendered.values["interface"]}],
    }
    users = list(dict.fromkeys(row["element"] for row in _query(model, request_body, tmp_path)))
    answer = _answer(rendered)
    assert users == answer["mentions_all"]
    assert answer["mentions_none"], "the model has components with no port of the interface"
    assert set(answer["mentions_none"]) <= set(model.names("swc")) - set(users)
    assert len(answer["mentions_none"]) <= BIND.NONE_COMPONENTS


@pytest.mark.parametrize("name", MODELS)
def test_12_the_components_are_the_where_on_the_interface_in_row_order(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    oracle_12_the_components_are_the_where_on_the_interface_in_row_order(
        request.getfixturevalue(name), bound[name]["12-components-on-an-interface"], tmp_path
    )


def oracle_13_the_unconnected_ports_are_the_products_derived_value_in_row_order(
    model: Any, rendered: Any, tmp_path: Path
) -> None:
    request_body = {
        "address": {"layer": "swc"},
        "members": "ports",
        "form": "index",
        "derived": ["unconnected"],
        "where": [{"field": "derived.unconnected.direction", "exists": True}],
    }
    pairs = [{"owner": row["element"], "name": row["member"]} for row in _query(model, request_body, tmp_path)]
    assert pairs == _answer(rendered)["with_owner"]


@pytest.mark.parametrize("name", MODELS)
def test_13_the_unconnected_ports_are_the_products_derived_value_in_row_order(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    oracle_13_the_unconnected_ports_are_the_products_derived_value_in_row_order(
        request.getfixturevalue(name), bound[name]["13-unconnected-ports"], tmp_path
    )


def oracle_15_the_components_are_the_partitions_derived_value_in_row_order(
    model: Any, rendered: Any, tmp_path: Path
) -> None:
    rows = _query(model, {"address": {"layer": "swc"}, "form": "index", "derived": ["partitions"]}, tmp_path)
    placed: dict[str, list[str]] = {}
    for row in rows:
        values = row["derived"]["partitions"]
        for value in values if isinstance(values, list) else [values] if values else []:
            placed.setdefault(value["partition"], []).append(row["element"])
    partition = rendered.values["partition"]
    answer = _answer(rendered)
    assert placed[partition] == answer["mentions_all"]
    assert len(placed[partition]) == max(len(members) for members in placed.values())
    assert answer["mentions_none"], "the model has components outside the partition"
    assert set(answer["mentions_none"]) <= set(model.names("swc")) - set(placed[partition])
    assert len(answer["mentions_none"]) <= BIND.NONE_OTHERS


@pytest.mark.parametrize("name", MODELS)
def test_15_the_components_are_the_partitions_derived_value_in_row_order(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    oracle_15_the_components_are_the_partitions_derived_value_in_row_order(
        request.getfixturevalue(name), bound[name]["15-components-in-a-partition"], tmp_path
    )


def _tasks_on(model: Any, core: str) -> list[str]:
    partitions = _cli(model, "query", "hardware", "--where", f"core={core}", "--format", "json")["elements"]
    return [
        task
        for partition in partitions
        for task in _cli(model, "query", "tasks", "--where", f"partition={partition}", "--format", "json")["elements"]
    ]


def oracle_16_the_tasks_are_the_hardware_and_tasks_reads(model: Any, rendered: Any, tmp_path: Path) -> None:
    core = rendered.values["core"]
    answer = _answer(rendered)
    assert sorted(_tasks_on(model, core), key=str) == answer["mentions_all"]
    cores = _cli(model, "query", "hardware", "--where", "kind=core", "--format", "json")["elements"]
    assert core in cores
    others = {other: _tasks_on(model, other) for other in cores if other != core}
    assert all(len(tasks) <= len(answer["mentions_all"]) for tasks in others.values())
    assert set(answer["mentions_none"]) <= {task for tasks in others.values() for task in tasks}
    assert len(answer["mentions_none"]) <= BIND.NONE_OTHERS


@pytest.mark.parametrize("name", MODELS)
def test_16_the_tasks_are_the_hardware_and_tasks_reads(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    oracle_16_the_tasks_are_the_hardware_and_tasks_reads(
        request.getfixturevalue(name), bound[name]["16-tasks-on-a-core"], tmp_path
    )


def oracle_18_the_sites_are_the_dependents_first_level(model: Any, rendered: Any, tmp_path: Path) -> None:
    long = isinstance(rendered, BIND.NotApplicable)
    if long:
        # More than twenty sites, which with_layer has no short form for.
        assert rendered == BIND.NotApplicable("long-site-list")
        requirement = BIND._most_linked_requirement(model)[0]
    else:
        requirement = rendered.values["requirement"]
    document = _cli(model, "query", "--dependents", requirement, "--depth", "1", "--format", "json")
    sites = []
    for entry in document["matched"]["requirement"]:
        if not entry["details"]:
            sites.append({"name": entry["name"], "layer": entry["layer"]})
        sites.extend(
            {"name": detail["value"], "layer": detail["kind"], "owner": entry["name"]} for detail in entry["details"]
        )
    if long:
        assert len(sites) > BIND.LONG_LIST_HEAD
        return
    answer = _answer(rendered)
    key = lambda site: (site["layer"], site["name"], site.get("owner", ""))  # noqa: E731
    assert sorted(answer["with_layer"], key=key) == sorted(sites, key=key)
    assert set(answer.get("mentions_none", [])).isdisjoint(
        {entry["name"] for entry in document["matched"]["requirement"]}
    )
    requirements = model.by_name("requirements")
    assert requirement in requirements and not requirements[requirement].stub


@pytest.mark.parametrize("name", MODELS)
def test_18_the_sites_are_the_dependents_first_level(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    oracle_18_the_sites_are_the_dependents_first_level(
        request.getfixturevalue(name), bound[name]["18-elements-satisfying-a-requirement"], tmp_path
    )


def test_21_the_requirements_are_the_committed_phrase_rule(bound: dict[str, dict[str, Any]]) -> None:
    rendered = bound["showcase"]["21-requirements-about-a-topic"]
    first, second = rendered.values["phrase"].split()
    model = CHECK.read_model(AGENT_TASK_BENCH)
    texts = {
        rid: f"{r.get('title', '')}\n{r.get('description', '')}"
        for rid, r in model["requirements"].items()
        if not r.get("stub")
    }
    word = r"(?<![A-Za-z0-9_]){}s?(?![A-Za-z0-9_])"
    phrase = re.compile(word.format(first) + r"\s+" + word.format(second), re.IGNORECASE)
    words = [re.compile(word.format(w), re.IGNORECASE) for w in (first, second)]
    holders = sorted((rid for rid, text in texts.items() if phrase.search(text)), key=str)
    answer = _answer(rendered)
    assert holders == answer["mentions_all"]
    assert 2 <= len(holders) <= len(texts) // 2
    assert (
        sorted((rid for rid, text in texts.items() if not any(w.search(text) for w in words)), key=str)
        == answer["mentions_none"]
    )
    titles = [text.partition("\n")[0] for text in texts.values()]
    assert any(re.search(word.format(first) + r"\s+" + word.format(second), t, re.IGNORECASE) for t in titles)


def _topic_counts(model: Any) -> dict[tuple[str, str], int]:
    """Every adjacent pair of title words under the committed rule, recomputed here, and how many hold it.

    A topic word is letters only, lower-cased, a plural ``s`` dropped (not after ``ss``, ``us``, ``is``),
    four letters or more; a requirement holds the pair when its title or description holds both
    adjacent, as whole words, each with an optional ``s``.
    """
    texts = {
        rid: f"{requirement.title}\n{requirement.description}"
        for rid, requirement in model.by_name("requirements").items()
        if not requirement.stub
    }

    def topic(word: str) -> str | None:
        if not re.fullmatch(r"[A-Za-z]+", word):
            return None
        word = word.lower()
        if word.endswith("s") and not word.endswith(("ss", "us", "is")):
            word = word[:-1]
        return word if len(word) >= 4 else None

    pairs = set()
    for text in texts.values():
        words = [topic(word) for word in text.partition("\n")[0].split()]
        pairs.update((a, b) for a, b in zip(words, words[1:], strict=False) if a and b)
    word = r"(?<![A-Za-z0-9_]){}s?(?![A-Za-z0-9_])"
    counts = {}
    for first, second in pairs:
        phrase = re.compile(word.format(re.escape(first)) + r"\s+" + word.format(re.escape(second)), re.IGNORECASE)
        counts[(first, second)] = sum(1 for text in texts.values() if phrase.search(text))
    return counts


@pytest.mark.parametrize("name", MODELS)
def test_21_the_phrase_is_the_commonest_pair_the_rule_admits_or_none_qualifies(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest
) -> None:
    model = request.getfixturevalue(name)
    declared = [rid for rid, requirement in model.by_name("requirements").items() if not requirement.stub]
    admitted = {pair: count for pair, count in _topic_counts(model).items() if 2 <= count <= len(declared) // 2}
    rendered = bound[name]["21-requirements-about-a-topic"]
    if not admitted:
        assert rendered == BIND.NotApplicable("no-topic")
        return
    best = max(admitted.values())
    chosen = tuple(rendered.values["phrase"].split())
    assert admitted[chosen] == best
    assert chosen == min(pair for pair, count in admitted.items() if count == best)


def test_none_of_leaves_out_a_candidate_either_way_inside_an_answer_name() -> None:
    assert BIND._none_of(["REQ-1.1", "REQ-1-2", "req-1", "REQ-10", "REQ-2"], ["REQ-1"], 10) == ["REQ-10", "REQ-2"]
    assert BIND._none_of(["REQ-1", "REQ-3"], ["REQ-1.1"], 10) == ["REQ-3"]
    assert BIND._none_of(["A", "B", "C"], [], 2) == ["A", "B"]


def test_a_requirement_id_inside_another_makes_the_requirement_lists_not_applicable(
    showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    """``REQ-1`` inside ``REQ-1.1``: a reply naming only the longer id would meet the shorter one's mention."""
    unallocated = _answer(bound["showcase"]["19-unallocated-requirements"])["mentions_all"]
    project = BIND.scratch_project(showcase, tmp_path / "project")
    parent = showcase.by_name("requirements")[unallocated[0]]
    child = parent.model_copy(update={"id": f"{parent.id}.1"})
    BIND.write_element(project / "model", "requirements", child, showcase.registry)
    model = BIND.BoundModel.load(project / "swcraft.yaml")
    templates = _templates()
    assert BIND.bind(model, templates["19-unallocated-requirements"]) == BIND.NotApplicable("nested-requirement-id")
    assert BIND._nested_ids(["REQ-1"], ["REQ-1", "REQ-1.1"])
    assert not BIND._nested_ids(["REQ-1"], ["REQ-1", "REQ-10", "req-1"])


def test_11_is_not_applicable_where_the_largest_component_has_no_port(tmp_path: Path) -> None:
    (tmp_path / "swcraft.yaml").write_text(agent_task_bench_module().MANIFEST, encoding="utf-8", newline="\n")
    (tmp_path / "model").mkdir()
    registry = current_registry()
    lonely = BIND.SWComponent.model_validate({"name": "Lonely", "type": "application"})
    BIND.write_element(tmp_path / "model", "swc", lonely, registry, {"group": "lonely"})
    model = BIND.BoundModel.load(tmp_path / "swcraft.yaml")
    assert model.names("swc") == ["Lonely"]
    assert BIND.bind(model, _templates()["11-ports-of-a-component"]) == BIND.NotApplicable("no-component")


#: The two-group model's groups: ``alpha`` holds the ``Zq`` components, ``beta`` the ``Aq`` ones, so
#: the read order (group first) is the reverse of name order.
TWO_GROUPS = {"alpha": "Zq", "beta": "Aq"}


@pytest.fixture
def two_groups(tmp_path: Path) -> Any:
    """24 unconnected component ports over two ``swc`` groups whose read order is not name order.

    Every list of 08, 12 and 13 is longer than the long-list head.
    """
    (tmp_path / "swcraft.yaml").write_text(agent_task_bench_module().MANIFEST, encoding="utf-8", newline="\n")
    model_root = tmp_path / "model"
    model_root.mkdir()
    registry = current_registry()
    for interface in ("IfZq", "IfOther"):
        element = BIND.SRInterface.model_validate(
            {
                "name": interface,
                "kind": "sender_receiver",
                "data_elements": [{"name": "value", "application_type": "ZqT"}],
            }
        )
        BIND.write_element(model_root, "interfaces", element, registry)
    for group, stem in TWO_GROUPS.items():
        for index in range(12):
            name = f"{stem}{index:02d}"
            interface = "IfOther" if index == 11 else "IfZq"
            component = BIND.SWComponent.model_validate(
                {
                    "name": name,
                    "type": "application",
                    "ports": [{"name": f"r{name}", "direction": "require", "interface": interface}],
                }
            )
            BIND.write_element(model_root, "swc", component, registry, {"group": group})
    return BIND.BoundModel.load(tmp_path / "swcraft.yaml")


def test_row_order_is_the_read_order_not_name_order(two_groups: Any, tmp_path: Path) -> None:
    """A layer read answers group ``alpha`` before ``beta``; the binder's lists follow it, so the head is the read's."""
    rows = _query(two_groups, {"address": {"layer": "swc"}}, tmp_path)
    read = [row if isinstance(row, str) else row["name"] for row in rows]
    assert read == two_groups.names("swc")
    assert read != sorted(read, key=str), "the fixture's read order is not name order"
    rendered = {template_id: BIND.bind(two_groups, template) for template_id, template in _templates().items()}
    oracle_08_the_direct_users_are_impacts_first_level_in_its_order(
        two_groups, rendered["08-interface-change-impact"], tmp_path
    )
    oracle_12_the_components_are_the_where_on_the_interface_in_row_order(
        two_groups, rendered["12-components-on-an-interface"], tmp_path
    )
    oracle_13_the_unconnected_ports_are_the_products_derived_value_in_row_order(
        two_groups, rendered["13-unconnected-ports"], tmp_path
    )
    unconnected = _answer(rendered["13-unconnected-ports"])
    assert unconnected["long_list"] and len(unconnected["with_owner"]) == 24
    owners = CHECK.member_owners(CHECK.read_model(two_groups.project))
    # The profile's reply (the number and a ``limit: 20`` read's first twenty) passes; the same
    # reply in name order fails the long-list rule.
    ports = [f"{row['element']}.{row['member']}" for row in _query(
        two_groups,
        {"address": {"layer": "swc"}, "members": "ports", "form": "index", "derived": ["unconnected"],
         "where": [{"field": "derived.unconnected.direction", "exists": True}], "limit": 20},
        tmp_path,
    )]  # fmt: skip
    assert len(ports) == 20
    short = f"There are 24 unconnected component ports. The first 20: {', '.join(ports)}, and 4 more."
    assert CHECK.check_answer_coded(short, unconnected, owners) == []
    by_name = sorted(f"{pair['owner']}.{pair['name']}" for pair in unconnected["with_owner"])[:20]
    wrong = f"There are 24 unconnected component ports. The first 20: {', '.join(by_name)}, and 4 more."
    assert [failure.code for failure in CHECK.check_answer_coded(wrong, unconnected, owners)] == ["answer.long_list"]


def _one_port_each(root: Path, ports: list[str]) -> Any:
    """One component per entry of *ports*, each with one unconnected require port of that name."""
    root.mkdir(parents=True)
    (root / "swcraft.yaml").write_text(agent_task_bench_module().MANIFEST, encoding="utf-8", newline="\n")
    registry = current_registry()
    interface = BIND.SRInterface.model_validate(
        {"name": "IfZq", "kind": "sender_receiver", "data_elements": [{"name": "value", "application_type": "ZqT"}]}
    )
    BIND.write_element(root / "model", "interfaces", interface, registry)
    for index, port in enumerate(ports):
        component = BIND.SWComponent.model_validate(
            {
                "name": f"Swc{index:02d}",
                "type": "application",
                "ports": [{"name": port, "direction": "require", "interface": "IfZq"}],
            }
        )
        BIND.write_element(root / "model", "swc", component, registry, {"group": "alpha"})
    return BIND.BoundModel.load(root / "swcraft.yaml")


def _check_13(model: Any, reply: str) -> list[str]:
    """The checker's codes on *reply* to 13 bound on *model*, the way a run checks it (the model's owners known)."""
    task = BIND.render_task(
        _templates()["13-unconnected-ports"], BIND.bind(model, _templates()["13-unconnected-ports"])
    )
    failures = CHECK.check_task_coded(model.project, task, answer=reply, baseline=model.project)
    return [failure.code for failure in failures]


def test_13_a_reply_that_swaps_two_components_ports_fails_where_port_names_repeat(tmp_path: Path) -> None:
    """Swc00's and Swc01's ports swapped, every name kept, fails."""
    model = _one_port_each(tmp_path / "short", ["pShared", "pOwn", "pShared"])
    answer = _answer(BIND.bind(model, _templates()["13-unconnected-ports"]))
    assert answer == {
        "with_owner": [
            {"owner": "Swc00", "name": "pShared"},
            {"owner": "Swc01", "name": "pOwn"},
            {"owner": "Swc02", "name": "pShared"},
        ]
    }
    right = "- Swc00.pShared\n- Swc01.pOwn\n- Swc02.pShared\n"
    swapped = "- Swc00.pOwn\n- Swc01.pShared\n- Swc02.pShared\n"
    assert _check_13(model, right) == []
    assert _check_13(model, swapped) == ["answer.with_owner"] * 4
    # The port names alone (``mentions_all``) pass the swapped reply.
    assert CHECK.check_answer_coded(swapped, {"mentions_all": ["pShared", "pOwn", "pShared"]}) == []


def test_13_a_long_list_whose_head_repeats_a_port_name_binds_in_the_short_form(tmp_path: Path) -> None:
    """Above the head, a shared name is asked once per component: the short form gives each pair."""
    ports = [f"pPort{index:02d}" for index in range(24)]
    ports[5] = "pShared"
    ports[9] = "pShared_"  # read like pShared once the Markdown drop is applied, told apart by its spelling
    model = _one_port_each(tmp_path / "repeated", ports)
    answer = _answer(BIND.bind(model, _templates()["13-unconnected-ports"]))
    assert answer["long_list"] and len(answer["with_owner"]) == 24
    pairs = [f"Swc{index:02d}.{port}" for index, port in enumerate(ports)]
    reply = f"There are 24 unconnected component ports. The first 20: {', '.join(pairs[:20])}, and 4 more."
    assert _check_13(model, reply) == []
    # A mention is the port it spells exactly.
    pairs[5], pairs[9] = "Swc05.pShared_", "Swc09.pShared"
    assert _check_13(model, f"24 ports: {', '.join(pairs[:20])}, and 4 more.") == [
        "answer.long_list",
        "answer.with_owner",
        "answer.with_owner",
    ]
    pairs[5], pairs[9] = "Swc06.pShared", "Swc10.pShared"
    assert _check_13(model, f"24 ports: {', '.join(pairs[:20])}, and 4 more.") == ["answer.long_list"]


def test_a_numeric_requirement_id_is_never_listed_nor_refused(
    showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    """``3`` as an id would be met, or refused, by a reply's count.

    19 is not applicable, and no ``mentions_none`` names such an id.
    """
    unallocated = _answer(bound["showcase"]["19-unallocated-requirements"])["mentions_all"]
    project = BIND.scratch_project(showcase, tmp_path / "project")
    numbered = showcase.by_name("requirements")[unallocated[0]].model_copy(update={"id": "3"})
    BIND.write_element(project / "model", "requirements", numbered, showcase.registry)
    model = BIND.BoundModel.load(project / "swcraft.yaml")
    assert BIND.bind(model, _templates()["19-unallocated-requirements"]) == BIND.NotApplicable("numeric-requirement-id")
    assert BIND._none_of(["3", "120", "1.2", "REQ-3", "SWR-12"], ["REQ-1"], 10) == ["REQ-3", "SWR-12"]
    # Why: a right reply's count meets the numeric id.
    failures = CHECK.check_answer_coded("There are 3 unallocated requirements: 7, 9 and 12.", {"mentions_none": ["3"]})
    assert [failure.code for failure in failures] == ["answer.mentions_none"]


def test_an_underscore_name_inside_another_is_left_out_of_mentions_none() -> None:
    """The checker reads ``Brake__Ctl`` as ``BrakeCtl``: a ``mentions_none`` of it would refuse the right reply."""
    assert BIND._none_of(["BrakeCtl", "Swc", "Other"], ["Brake__Ctl", "Swc_"], 10) == ["Other"]


def test_bind_all_with_no_template_binds_none(showcase: Any) -> None:
    assert BIND.bind_all(showcase, []) == {}


def test_a_check_model_run_that_answers_no_findings_counts_as_one_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unparseable(*_arguments: Any, **_options: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "x", "")

    monkeypatch.setattr(BIND.subprocess, "run", unparseable)
    assert BIND.check_model_error_count(tmp_path) == 1


@pytest.mark.parametrize("name", MODELS)
def test_a_list_over_twenty_opts_into_the_long_list_rule_and_no_other_does(
    name: str, bound: dict[str, dict[str, Any]]
) -> None:
    for template_id, rendered in _questions(bound[name]).items():
        answer = _answer(rendered)
        assert answer.get("long_list", False) == (len(answer.get("mentions_all", [])) > BIND.LONG_LIST_HEAD), (
            template_id
        )


def test_the_specimen_exercises_the_long_list_rule(bound: dict[str, dict[str, Any]]) -> None:
    long = {
        template_id
        for template_id, rendered in _questions(bound["specimen"]).items()
        if _answer(rendered).get("long_list")
    }
    assert long == {
        "08-interface-change-impact",
        "11-ports-of-a-component",
        "12-components-on-an-interface",
        "15-components-in-a-partition",
    }


@pytest.mark.parametrize(
    "template_id",
    [
        "11-ports-of-a-component",
        "12-components-on-an-interface",
        "13-unconnected-ports",
        "15-components-in-a-partition",
        "16-tasks-on-a-core",
    ],
)
def test_where_the_binding_picks_the_committed_target_the_answer_is_the_committed_one(
    template_id: str, bound: dict[str, dict[str, Any]]
) -> None:
    rendered = bound["showcase"][template_id]
    task = BIND.render_task(_templates()[template_id], rendered)
    committed = _committed(template_id)
    if template_id != "13-unconnected-ports":
        # 13's prompt says *component* ports; the committed one asks for every port.
        assert task["prompt"] == committed["prompt"]
    answer, expected = task["expect"]["answer"], committed["expect"]["answer"]
    if template_id == "13-unconnected-ports":
        # The committed task names the ports alone; on the bench each port name is one component's.
        assert [pair["name"] for pair in answer["with_owner"]] == expected["mentions_all"]
    else:
        assert answer["mentions_all"] == expected["mentions_all"]
    assert answer.get("number") == expected.get("number")
    assert set(answer.get("mentions_none", [])) == set(expected.get("mentions_none", []))


@pytest.mark.parametrize("name", MODELS)
def test_a_rendered_question_passes_its_right_reply_and_fails_an_empty_one(
    name: str, bound: dict[str, dict[str, Any]]
) -> None:
    for template_id, rendered in _questions(bound[name]).items():
        answer = _answer(rendered)
        assert CHECK.check_answer_coded(_right_reply(answer), answer) == [], template_id
        assert CHECK.check_answer_coded("I could not tell.", answer), template_id


# ---------------------------------------------------------------------------
# The product-rule questions (09, 14, 17, 19, 20, 22), held to the CLI's own answers.
# ---------------------------------------------------------------------------

_RESOURCE = re.compile(r"^RESOURCE (\S+) \((core|memory)")
_CONSUMPTION = re.compile(r"^  CONSUMPTION (\S+) bytes of a (\S+) byte budget \(headroom (\S+) bytes\)$")
_CONSUMER = re.compile(r"^    - (\S+): (\d+) bytes$")


def memory_blocks(model: Any) -> dict[str, dict[str, Any]]:
    """``swcraft resource-report``'s memory blocks: each one's three figures and its consumers' bytes."""
    completed = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "resource-report", "--project", model.manifest.as_posix()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    blocks: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for line in completed.stdout.splitlines():
        resource = _RESOURCE.match(line)
        if resource is not None:
            current = blocks.setdefault(resource.group(1), {"consumers": {}}) if resource.group(2) == "memory" else None
            continue
        if current is None:
            continue
        consumption = _CONSUMPTION.match(line)
        if consumption is not None:
            current.update(zip(("consumed", "budget", "headroom"), consumption.groups(), strict=True))
        consumer = _CONSUMER.match(line)
        if consumer is not None:
            current["consumers"][consumer.group(1)] = int(consumer.group(2))
    return blocks


def _check_model_findings(model: Any, rule: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "check-model", "--format", "json",
         "--project", model.manifest.as_posix()],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )  # fmt: skip
    return [finding for finding in json.loads(completed.stdout)["findings"] if finding["rule"] == rule]


def oracle_09_the_figures_are_the_resource_reports_and_the_size_fits_under_neither_reading(
    model: Any, rendered: Any, tmp_path: Path
) -> None:
    (memory,) = (key for key in rendered.reference if key not in ("requested", "computed_by"))
    block = memory_blocks(model)[memory]
    figures = rendered.reference[memory]
    assert {key: figures[key] for key in ("consumed", "budget", "headroom")} == {
        key: block[key] for key in ("consumed", "budget", "headroom")
    }
    # The smallest size past the headroom under both readings of a KB.
    headroom = Fraction(block["headroom"])
    size = int(rendered.values["size_kb"])
    assert all(size * reading > headroom for reading in BIND.KILOBYTE_READINGS)
    assert size == 1 or (size - 1) * min(BIND.KILOBYTE_READINGS) <= headroom
    assert _answer(rendered) == {"first_of": {"candidates": ["yes", "no"], "expected": "no"}}
    hardware = model.by_name("hardware")
    rams = [
        element
        for element in hardware.values()
        if getattr(element, "type", None) == "RAM" and rendered.values["core"] in element.accessible
    ]
    assert [element.name for element in rams] == [memory]


@pytest.mark.parametrize("name", MODELS)
def test_09_the_figures_are_the_resource_reports_and_the_size_fits_under_neither_reading(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    oracle_09_the_figures_are_the_resource_reports_and_the_size_fits_under_neither_reading(
        request.getfixturevalue(name), bound[name]["09-ram-for-a-component"], tmp_path
    )


def test_09_on_the_showcase_binds_the_committed_core_at_the_first_size_past_its_headroom(
    bound: dict[str, dict[str, Any]],
) -> None:
    rendered = bound["showcase"]["09-ram-for-a-component"]
    committed = _committed("09-ram-for-a-component")
    assert rendered.values["core"] == "Core0"
    assert rendered.reference["RAM"]["headroom"] == str(committed["reference"]["RAM"]["headroom"])
    # 10 240 bytes of headroom: 11 KB is past it under both readings, 10 KB is not.
    assert rendered.values["size_kb"] == "11"


def oracle_14_the_count_is_the_effective_levels_query(model: Any, rendered: Any, tmp_path: Path) -> None:
    rows = _query(model, {"address": {"layer": "swc"}, "form": "index", "derived": ["effective_level"]}, tmp_path)
    counts: dict[str, int] = {}
    for row in rows:
        rating = row["derived"]["effective_level"]["rating"]
        counts[rating] = counts.get(rating, 0) + 1
    level = rendered.values["level"]
    assert counts[level] == _answer(rendered)["number"]
    assert counts[level] == max(count for rating, count in counts.items() if rating != "QM")


@pytest.mark.parametrize("name", _applicable("14-asil-b-count"))
def test_14_the_count_is_the_effective_levels_query(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    oracle_14_the_count_is_the_effective_levels_query(
        request.getfixturevalue(name), bound[name]["14-asil-b-count"], tmp_path
    )


def test_14_on_the_showcase_is_the_committed_task(bound: dict[str, dict[str, Any]]) -> None:
    task = BIND.render_task(_templates()["14-asil-b-count"], bound["showcase"]["14-asil-b-count"])
    committed = _committed("14-asil-b-count")
    assert task["prompt"] == committed["prompt"]
    assert task["expect"] == committed["expect"]


def test_17_the_largest_consumer_is_the_resource_reports(showcase: Any, bound: dict[str, dict[str, Any]]) -> None:
    rendered = bound["showcase"]["17-largest-ram-consumer"]
    consumers = {
        component: amount
        for component, amount in memory_blocks(showcase)[rendered.values["memory"]]["consumers"].items()
        if amount
    }
    counts = {
        memory: sum(1 for amount in block["consumers"].values() if amount)
        for memory, block in memory_blocks(showcase).items()
    }
    assert counts[rendered.values["memory"]] == max(counts.values())
    rule = _answer(rendered)["first_of"]
    largest = max(consumers.values())
    assert [component for component, amount in consumers.items() if amount == largest] == [rule["expected"]]
    assert set(consumers) <= set(rule["candidates"])
    committed = _committed("17-largest-ram-consumer")
    assert BIND.render_task(_templates()["17-largest-ram-consumer"], rendered)["prompt"] == committed["prompt"]
    assert rule["expected"] == committed["expect"]["answer"]["first_of"]["expected"]
    assert rendered.reference["consumers"] == committed["reference"]["consumers"]


def test_17_is_a_tie_where_two_consumers_share_the_largest_figure(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _memory_of(monkeypatch, "Ram", {"Alpha": 20, "Beta": 20, "Gamma": 5})
    assert BIND.largest_consumer_of_the_busiest_memory(model) == BIND.NotApplicable("tie")


def _memory_of(monkeypatch: pytest.MonkeyPatch, memory: str, consumers: dict[str, int]) -> Any:
    """17's resource report replaced by one memory *memory* with *consumers* (name: amount)."""
    from types import SimpleNamespace

    usage = SimpleNamespace(
        name=memory,
        consumers=[SimpleNamespace(name=name, amount=amount, declared=True) for name, amount in consumers.items()],
    )
    monkeypatch.setattr(BIND, "resource_report", lambda root: SimpleNamespace(analysis=SimpleNamespace(resources=[])))
    monkeypatch.setattr(BIND, "_memories_with_consumers", lambda usages: [(usage, list(consumers))])
    return SimpleNamespace(model_root=Path("unused"), instance_words=lambda types: [])


def test_17_asks_only_a_memory_whose_every_consumer_declared_an_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    """A consumer with no declared amount leaves the largest unknown: a reply saying so is right."""
    from types import SimpleNamespace

    model = _memory_of(monkeypatch, "Ram", {"Winner": 20, "Other": 10})
    undeclared = SimpleNamespace(name="Unbudgeted", amount=None, declared=False)
    busiest = SimpleNamespace(
        name="Ram",
        consumers=[SimpleNamespace(name="Winner", amount=20, declared=True), undeclared],
    )
    quieter = SimpleNamespace(name="Rom", consumers=[SimpleNamespace(name="Second", amount=5, declared=True)])
    monkeypatch.setattr(BIND, "_memories_with_consumers", lambda usages: [(busiest, ["Winner"])])
    assert BIND.largest_consumer_of_the_busiest_memory(model) == BIND.NotApplicable("undeclared-consumer")
    monkeypatch.setattr(BIND, "_memories_with_consumers", lambda usages: [(busiest, ["Winner"]), (quieter, ["Second"])])
    rendered = BIND.largest_consumer_of_the_busiest_memory(model)
    assert rendered.values == {"memory": "Rom"}
    assert _answer(rendered)["first_of"]["expected"] == "Second"


def test_17_keeps_its_winner_and_leaves_out_another_consumer_named_like_the_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _memory_of(monkeypatch, "Brake", {"Wiper": 20, "Brake": 10, "LampCtrl": 5})
    rendered = BIND.largest_consumer_of_the_busiest_memory(model)
    rule = _answer(rendered)["first_of"]
    assert rule == {"candidates": ["LampCtrl", "Wiper"], "expected": "Wiper"}
    assert CHECK.check_answer("Wiper takes the most of Brake.", _answer(rendered)) == []
    assert CHECK.check_answer("LampCtrl takes the most of Brake.", _answer(rendered))


def test_17_is_not_applicable_where_a_consumer_reads_as_its_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    # Consumer and Consumer_ read alike, so the earlier candidate would win at Consumer_'s place.
    model = _memory_of(monkeypatch, "Ram", {"Consumer_": 20, "Consumer": 10})
    assert BIND.largest_consumer_of_the_busiest_memory(model) == BIND.NotApplicable("indistinct-consumers")


def test_17_leaves_out_a_losing_consumer_spelt_as_an_ordinary_word(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _memory_of(monkeypatch, "Ram", {"Winner": 20, "Component": 10, "SwcBeta": 5})
    rendered = BIND.largest_consumer_of_the_busiest_memory(model)
    assert _answer(rendered)["first_of"] == {"candidates": ["SwcBeta", "Winner"], "expected": "Winner"}
    assert CHECK.check_answer("The component Winner takes the most of Ram.", _answer(rendered)) == []
    assert CHECK.check_answer("SwcBeta takes the most of Ram.", _answer(rendered))


def test_17_leaves_out_a_losing_consumer_named_like_a_layer_word_or_an_acronym(monkeypatch: pytest.MonkeyPatch) -> None:
    # "The RAM consumer WinnerSwc" states a layer before the name: a loser named RAM would lead.
    model = _memory_of(monkeypatch, "RAM1", {"WinnerSwc": 20, "RAM": 10, "ECU": 8, "Core": 6, "SwcBeta": 5})
    rendered = BIND.largest_consumer_of_the_busiest_memory(model)
    assert _answer(rendered)["first_of"] == {"candidates": ["SwcBeta", "WinnerSwc"], "expected": "WinnerSwc"}
    assert CHECK.check_answer("The RAM consumer WinnerSwc takes the most of RAM1.", _answer(rendered)) == []
    assert CHECK.check_answer("SwcBeta takes the most of RAM1.", _answer(rendered))


def test_17_bound_leaves_out_a_losing_consumer_named_like_a_word_of_the_rendered_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # wHICH is no ordinary word, no layer word and no acronym, but the prompt opens with "Which".
    model = _memory_of(monkeypatch, "Ram9", {"WinnerSwc": 20, "wHICH": 10, "SwcBeta": 5})
    rendered = BIND.bind(model, _templates()["17-largest-ram-consumer"])
    assert _answer(rendered)["first_of"]["candidates"] == ["SwcBeta", "WinnerSwc"]
    assert CHECK.check_answer("Which component? WinnerSwc takes the most of Ram9.", _answer(rendered)) == []


def test_17_leaves_out_a_losing_consumer_named_like_a_long_acronym(monkeypatch: pytest.MonkeyPatch) -> None:
    # No length cutoff: "The AUTOSAR component WinnerSwc" would lead with a loser named AUTOSAR.
    model = _memory_of(monkeypatch, "RAM1", {"WinnerSwc": 20, "AUTOSAR": 10, "CAN2": 8, "SwcBeta": 5})
    rendered = BIND.largest_consumer_of_the_busiest_memory(model)
    assert _answer(rendered)["first_of"] == {"candidates": ["SwcBeta", "WinnerSwc"], "expected": "WinnerSwc"}
    assert CHECK.check_answer("The AUTOSAR component WinnerSwc takes the most of RAM1.", _answer(rendered)) == []


@pytest.mark.parametrize(
    ("name", "prose"),
    [("RAM", True), ("ECU", True), ("Core", True), ("Other", True), ("AUTOSAR", True), ("CAN2", True),
     ("AUTOSAR_", True), ("WinnerSwc", False), ("Swc1", False), ("REQ-001", False), ("BRAKE_CTL", False),
     ("2024", False)],
)  # fmt: skip
def test_a_prose_word_is_ordinary_a_layer_word_or_an_acronym(name: str, prose: bool) -> None:
    assert BIND._prose_word(name) is prose


def test_09_keeps_its_answer_word_candidates(bound: dict[str, dict[str, Any]]) -> None:
    rendered = bound["showcase"]["09-ram-for-a-component"]
    assert _answer(rendered)["first_of"]["candidates"] == ["yes", "no"]


@pytest.mark.parametrize(
    ("core", "memory", "applicable"),
    [("Yes", "Ram0", False), ("NO", "Ram0", False), ("Core-Yes", "Ram0", False), ("Core0", "No", False),
     ("Core0", "Ram0", True), ("Yes_Core", "Ram0", True), ("Nova", "Ram0", True)],
)  # fmt: skip
def test_09_is_not_applicable_where_the_core_or_its_memory_reads_as_an_answer_word(
    monkeypatch: pytest.MonkeyPatch, core: str, memory: str, applicable: bool
) -> None:
    # "Core Yes does not have enough RAM" would lead with "yes": a right reply scored wrong.
    from types import SimpleNamespace

    class FakeMemory(SimpleNamespace):
        pass

    ram = FakeMemory(name=memory, type="RAM", threshold=0.9, size="64 KB")
    usage = SimpleNamespace(
        name=memory, resource=ram, consumers=[SimpleNamespace(name="Swc", amount=10, declared=True)],
        limit=100, consumption=10, headroom=Fraction(90),
    )  # fmt: skip
    cpu = SimpleNamespace(name=core, resource=SimpleNamespace(), consumers=[])
    report = SimpleNamespace(analysis=SimpleNamespace(resources=[cpu, usage]))
    monkeypatch.setattr(BIND, "Memory", FakeMemory)
    monkeypatch.setattr(BIND, "resource_report", lambda root: report)
    monkeypatch.setattr(BIND, "reachable_memories", lambda analysis, name: [usage])
    monkeypatch.setattr(BIND, "budget_status", lambda found: "ok")
    monkeypatch.setattr(BIND, "fits", lambda found, amount: amount <= found.headroom)
    rendered = BIND.ram_left_on_a_core(SimpleNamespace(model_root=Path("unused")))
    if applicable:
        assert rendered.values["core"] == core
        assert _answer(rendered)["first_of"] == {"candidates": ["yes", "no"], "expected": "no"}
    else:
        assert rendered == BIND.NotApplicable("core-named-like-an-answer-word")
    assert "core-named-like-an-answer-word" in BIND.NOT_APPLICABLE


@pytest.mark.parametrize(
    ("name", "plain"),
    [("Other", True), ("component", True), ("Other_", True), ("RAM", False), ("Swc1", False),
     ("BrakeCtrl", False), ("Brake_Ctrl", False)],
)  # fmt: skip
def test_a_plain_word_is_letters_only_with_no_inner_capital(name: str, plain: bool) -> None:
    assert BIND._plain_word(name) is plain


def test_17_is_not_applicable_where_its_winner_is_named_like_the_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reply naming the memory would lead with the winner, a wrong one included.
    model = _memory_of(monkeypatch, "Brake", {"Brake": 20, "Wiper": 10})
    assert BIND.largest_consumer_of_the_busiest_memory(model) == BIND.NotApplicable("consumer-named-like-memory")


def test_a_mentions_none_name_like_a_prompt_or_layer_word_is_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    template = _templates()["12-components-on-an-interface"]
    rendered = BIND.Rendered(
        values={"interface": "IStatus"},
        expect={"model": "unchanged", "answer": {"mentions_all": ["Provider"], "mentions_none": [
            "Interface", "Have", "Ports", "Other", "Provider_", "OtherSwc"]}},
        reference={},
    )  # fmt: skip
    monkeypatch.setitem(BIND.BINDINGS, template["binding"], lambda model: rendered)
    bound = BIND.bind(None, template)
    assert _answer(bound)["mentions_none"] == ["OtherSwc"], (
        "a word of the prompt, a layer word, an ordinary word or an expected name's spelling is never refused"
    )
    reply = "Provider is the only component using interface IStatus; it has ports. No other components use IStatus."
    assert CHECK.check_answer(reply, _answer(bound)) == []
    only_prose = dataclasses.replace(rendered, expect={"model": "unchanged", "answer": {
        "mentions_all": ["Provider"], "mentions_none": ["Interface"]}})  # fmt: skip
    monkeypatch.setitem(BIND.BINDINGS, template["binding"], lambda model: only_prose)
    assert "mentions_none" not in _answer(BIND.bind(None, template)), "nothing left, the key goes"


def test_a_copied_manifest_s_absolute_outputs_inside_the_project_become_relative(tmp_path: Path) -> None:
    from swcraft_tools.project import require_project

    project = tmp_path / "project"
    shutil.copytree(AGENT_TASK_BENCH, project)
    manifest = project / "swcraft.yaml"
    text = manifest.read_text(encoding="utf-8")
    text = text.replace("arxml: dist/model.arxml", f"arxml: {(project / 'dist' / 'model.arxml').as_posix()}")
    text = text.replace("needs: dist/needs.json", f"needs: {(project / 'dist' / 'needs.json').as_posix()}")
    manifest.write_text(text, encoding="utf-8", newline="\n")
    require_project(manifest.as_posix())
    copy = tmp_path / "copy"
    BIND.copy_project(manifest, copy, keep_relative=True)
    outputs = load_yaml_document((copy / BIND.MANIFEST).read_text(encoding="utf-8"))["outputs"]
    assert outputs == {"arxml": "dist/model.arxml", "needs": "dist/needs.json"}
    require_project((copy / BIND.MANIFEST).as_posix())


def oracle_19_the_unallocated_requirements_are_check_models_findings(model: Any, rendered: Any, tmp_path: Path) -> None:
    answer = _answer(rendered)
    unallocated = [
        finding["subjects"][0]["element"] for finding in _check_model_findings(model, "trace.requirement-unallocated")
    ]
    assert unallocated == answer["mentions_all"]
    stubs = {finding["subjects"][0]["element"] for finding in _check_model_findings(model, "trace.requirement-stub")}
    assert set(answer["mentions_none"]).isdisjoint(set(unallocated) | stubs)
    assert set(answer["mentions_none"]) <= set(model.names("requirements"))


@pytest.mark.parametrize("name", MODELS)
def test_19_the_unallocated_requirements_are_check_models_findings(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    oracle_19_the_unallocated_requirements_are_check_models_findings(
        request.getfixturevalue(name), bound[name]["19-unallocated-requirements"], tmp_path
    )


def test_19_on_the_showcase_is_the_committed_list(bound: dict[str, dict[str, Any]]) -> None:
    answer = _answer(bound["showcase"]["19-unallocated-requirements"])
    assert answer["mentions_all"] == _committed("19-unallocated-requirements")["expect"]["answer"]["mentions_all"]


def oracle_20_the_links_are_check_models_needs_confirmation_findings(model: Any, rendered: Any, tmp_path: Path) -> None:
    findings = _check_model_findings(model, "trace.link-needs-confirmation")
    assert findings, "the model holds a link that needs confirmation"
    requirements = sorted({finding["target"] for finding in findings}, key=str)
    owners = sorted({finding["subjects"][0]["element"] for finding in findings}, key=str)
    assert _answer(rendered)["mentions_all"] == [
        *requirements,
        *(owner for owner in owners if owner not in requirements),
    ]


@pytest.mark.parametrize("name", MODELS)
def test_20_the_links_are_check_models_needs_confirmation_findings(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    oracle_20_the_links_are_check_models_needs_confirmation_findings(
        request.getfixturevalue(name), bound[name]["20-links-needing-confirmation"], tmp_path
    )


def test_20_on_the_showcase_is_the_committed_task(bound: dict[str, dict[str, Any]]) -> None:
    task = BIND.render_task(
        _templates()["20-links-needing-confirmation"], bound["showcase"]["20-links-needing-confirmation"]
    )
    committed = _committed("20-links-needing-confirmation")
    assert task["prompt"] == committed["prompt"]
    assert task["expect"] == committed["expect"]


def oracle_22_the_candidates_are_the_trace_searchs_places(model: Any, rendered: Any, tmp_path: Path) -> None:
    copy = tmp_path / "project"
    shutil.copytree(model.project, copy)
    completed = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "experiment", "trace", "--format", "json",
         "--project", (copy / "swcraft.yaml").as_posix()],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )  # fmt: skip
    entries = {entry["id"]: entry for entry in json.loads(completed.stdout)["requirements"]}
    entry = entries[rendered.values["requirement"]]
    places: dict[int, dict[str, Any]] = {}
    for candidate in entry["candidates"]:
        places.setdefault(candidate["place"], candidate["group"])
    rule = _answer(rendered)["candidates"]
    assert [places[place]["name"] for place in sorted(places)[: BIND.TOP_PLACES]] == rule["top"]
    for place in sorted(places)[: BIND.TOP_PLACES]:
        terms = {term for evidence in places[place]["evidence"] for term in evidence.get("terms", [])}
        assert rule["terms"][places[place]["name"]] and set(rule["terms"][places[place]["name"]]) <= terms
    # The first requirement by id that qualifies: none before it does.
    for rid in sorted(entries, key=str):
        if rid == rendered.values["requirement"]:
            break
        groups: dict[int, dict[str, Any]] = {}
        for candidate in entries[rid]["candidates"]:
            groups.setdefault(candidate["place"], candidate["group"])
        top = [groups[place] for place in sorted(groups)[: BIND.TOP_PLACES]]
        assert (
            len(top) < BIND.TOP_PLACES
            or len({group["name"] for group in top}) < BIND.TOP_PLACES
            or not all(any(evidence.get("terms") for evidence in group["evidence"]) for group in top)
        ), rid


@pytest.mark.parametrize("name", MODELS)
def test_22_the_candidates_are_the_trace_searchs_places(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    oracle_22_the_candidates_are_the_trace_searchs_places(
        request.getfixturevalue(name), bound[name]["22-candidates-for-a-requirement"], tmp_path
    )


def test_22_on_the_showcase_is_the_committed_task(bound: dict[str, dict[str, Any]]) -> None:
    task = BIND.render_task(
        _templates()["22-candidates-for-a-requirement"], bound["showcase"]["22-candidates-for-a-requirement"]
    )
    committed = _committed("22-candidates-for-a-requirement")
    assert task["prompt"] == committed["prompt"]
    assert task["expect"] == committed["expect"]


#: Each question template's recomputation by the product's own reads, on a model and the
#: template's rendering there; the large bench's bindings are held to it too. 17's oracle is the
#: showcase's committed task, 21's the committed phrase rule.
CLI_ORACLES = {
    "08-interface-change-impact": oracle_08_the_direct_users_are_impacts_first_level_in_its_order,
    "11-ports-of-a-component": oracle_11_the_ports_are_the_members_read_in_port_order,
    "12-components-on-an-interface": oracle_12_the_components_are_the_where_on_the_interface_in_row_order,
    "13-unconnected-ports": oracle_13_the_unconnected_ports_are_the_products_derived_value_in_row_order,
    "15-components-in-a-partition": oracle_15_the_components_are_the_partitions_derived_value_in_row_order,
    "16-tasks-on-a-core": oracle_16_the_tasks_are_the_hardware_and_tasks_reads,
    "18-elements-satisfying-a-requirement": oracle_18_the_sites_are_the_dependents_first_level,
    "09-ram-for-a-component": oracle_09_the_figures_are_the_resource_reports_and_the_size_fits_under_neither_reading,
    "14-asil-b-count": oracle_14_the_count_is_the_effective_levels_query,
    "19-unallocated-requirements": oracle_19_the_unallocated_requirements_are_check_models_findings,
    "20-links-needing-confirmation": oracle_20_the_links_are_check_models_needs_confirmation_findings,
    "22-candidates-for-a-requirement": oracle_22_the_candidates_are_the_trace_searchs_places,
}


def test_the_trace_search_writes_nothing_into_the_project(showcase: Any, tmp_path: Path) -> None:
    copy = tmp_path / "project"
    shutil.copytree(showcase.project, copy)
    before = sorted(path.relative_to(copy).as_posix() for path in copy.rglob("*"))
    BIND.bind(BIND.BoundModel.load(copy / "swcraft.yaml"), _templates()["22-candidates-for-a-requirement"])
    assert sorted(path.relative_to(copy).as_posix() for path in copy.rglob("*")) == before


# ---------------------------------------------------------------------------
# Not applicable.
# ---------------------------------------------------------------------------


def test_13_is_not_applicable_on_a_model_with_no_unconnected_port(minimal: Any) -> None:
    assert minimal.locator.unconnected_pairs() == ()
    results = BIND.bind_all(minimal)
    assert {
        template_id: result.code for template_id, result in results.items() if isinstance(result, BIND.NotApplicable)
    } == {
        "13-unconnected-ports": "no-unconnected-port",
        "21-requirements-about-a-topic": "no-topic",
        # One task per partition.
        "04-move-runnable": "no-task-pair",
        "36-call-asynchronously": "no-synchronous-call",
        **PRODUCT_RULE_NOT_APPLICABLE,
    }


def test_a_model_with_no_component_is_not_applicable_by_its_codes(tmp_path: Path) -> None:
    (tmp_path / "swcraft.yaml").write_text(agent_task_bench_module().MANIFEST, encoding="utf-8", newline="\n")
    (tmp_path / "model").mkdir()
    model = BIND.BoundModel.load(tmp_path / "swcraft.yaml")
    results = BIND.bind_all(model)
    # An empty model is check-model's error (no hardware layer), so the change templates stop at
    # the baseline; their bindings, asked directly, say what else is missing.
    templates = _templates()
    assert {template_id: BIND.BINDINGS[templates[template_id]["binding"]](model).code for template_id in CHANGES} == {
        "01-connect-two-components": "no-sender-receiver-interface",
        "03-new-interface-and-type": "no-mapping-set",
        "04-move-runnable": "no-task-pair",
        "05-remove-component": "no-ordinary-component",
        "06-rename-data-element": "no-sender-receiver-interface",
        "07-add-requirement": "no-require-port",
        "10-import-component": "no-provide-port-in-a-composition",
        "23-link-a-port-to-a-requirement": "no-linked-requirement",
        "24-confirm-links-at-a-new-version": "no-linked-requirement",
        "35-make-a-read-implicit": "no-sender-receiver-read",
        "36-call-asynchronously": "no-synchronous-call",
    }
    assert {template_id: result.code for template_id, result in results.items()} == {
        **CHANGES_ON_AN_ERROR,
        "08-interface-change-impact": "no-interface-named",
        "11-ports-of-a-component": "no-component",
        "12-components-on-an-interface": "no-interface-named",
        "13-unconnected-ports": "no-unconnected-port",
        "15-components-in-a-partition": "no-partition",
        "16-tasks-on-a-core": "no-core-with-tasks",
        "18-elements-satisfying-a-requirement": "no-linked-requirement",
        "21-requirements-about-a-topic": "no-topic",
        **PRODUCT_RULE_NOT_APPLICABLE,
    }


def test_the_partition_and_requirement_questions_are_not_applicable_on_a_model_with_neither(bare: Any) -> None:
    results = BIND.bind_all(bare)
    assert {
        template_id: result.code for template_id, result in results.items() if isinstance(result, BIND.NotApplicable)
    } == {
        "13-unconnected-ports": "no-unconnected-port",
        "15-components-in-a-partition": "no-partition",
        "16-tasks-on-a-core": "no-core-with-tasks",
        "18-elements-satisfying-a-requirement": "no-linked-requirement",
        "21-requirements-about-a-topic": "no-topic",
        # The requirements removed leave links naming none: check-model's errors.
        **CHANGES_ON_AN_ERROR,
        **PRODUCT_RULE_NOT_APPLICABLE,
    }


def test_a_model_without_the_optional_layers_in_a_path_with_spaces_binds_or_answers_a_code(
    tmp_path: Path,
) -> None:
    """An ARXML import's shape (no requirements, tasks, sequences, partitions or links) in a path with spaces.

    Every template binds or is not applicable by its code, and the subprocesses take the spaced path.
    """
    manifest = _specimen(tmp_path / "a project with spaces", MINIMAL_EXAMPLE)
    model_root = manifest.parent / "model"
    registry = current_registry()
    for layer in ("requirements", "tasks", "sequences"):
        for files in stored_element_files(model_root, layer, registry=registry).values():
            for layer_file in files:
                layer_file.path.unlink()
    loaded = BIND.BoundModel.load(manifest)
    for name, files in stored_element_files(model_root, "hardware", registry=registry).items():
        if isinstance(loaded.by_name("hardware")[name], Partition):
            for layer_file in files:
                layer_file.path.unlink()
    for element in loaded.by_name("swc").values():
        BIND.write_element(model_root, "swc", element.model_copy(update={"satisfies": []}), registry)
    model = BIND.BoundModel.load(manifest)
    assert model.baseline_error_count == 0
    results = BIND.bind_all(model)
    assert {
        template_id: result.code for template_id, result in results.items() if isinstance(result, BIND.NotApplicable)
    } == {
        "04-move-runnable": "no-task-pair",
        "13-unconnected-ports": "no-unconnected-port",
        "15-components-in-a-partition": "no-partition",
        "16-tasks-on-a-core": "no-core-with-tasks",
        "18-elements-satisfying-a-requirement": "no-linked-requirement",
        "21-requirements-about-a-topic": "no-topic",
        "23-link-a-port-to-a-requirement": "no-linked-requirement",
        "24-confirm-links-at-a-new-version": "no-linked-requirement",
        "36-call-asynchronously": "no-synchronous-call",
        **PRODUCT_RULE_NOT_APPLICABLE,
    }
    assert {template_id for template_id, result in results.items() if isinstance(result, BIND.Rendered)} == {
        "01-connect-two-components",
        "03-new-interface-and-type",
        "05-remove-component",
        "06-rename-data-element",
        "07-add-requirement",
        "08-interface-change-impact",
        "10-import-component",
        "11-ports-of-a-component",
        "12-components-on-an-interface",
        "35-make-a-read-implicit",
    }


# ---------------------------------------------------------------------------
# The right run and the change templates (01, 03, 04, 05, 06, 07, 23, 24).
# ---------------------------------------------------------------------------

#: The committed showcase tasks' own placeholder values and fixed texts, by which the committed
#: assertions are rewritten to compare their shape with the bound ones.
COMMITTED_VALUES = {
    "01-connect-two-components": {
        "provider": "DiagnosticManager",
        "provider_port": "pForwardedThermalStatus",
        "requirer": "InstrumentCluster",
        "requirer_port": "rForwardedThermalStatus",
        "interface": "IThermalProtectionStatus",
    },
    "03-new-interface-and-type": {
        "interface": "IBrakePadWear",
        "element": "padWear",
        "app_type": "BrakePadWear_Pct",
        "component": "BrakeController",
        "port": "pBrakePadWear",
        "implementation_type": "uint8",
        "mapping_set": "BrakeThermalMappings",
    },
    "07-add-requirement": {
        "requirement": "SWR-BRK-013",
        "component": "BrakeController",
        "port": "rPermittedBrakeAction",
        "title": "Derated brake request",
    },
}


def _task(template_id: str, rendered: Any) -> dict[str, Any]:
    return BIND.render_task(_templates()[template_id], rendered)


def _codes(project: Path, task: dict[str, Any], baseline: Path) -> list[str]:
    return [failure.code for failure in CHECK.check_task_coded(project, task, baseline=baseline)]


@pytest.mark.parametrize("name", MODELS)
def test_every_change_template_is_offered(name: str, bound: dict[str, dict[str, Any]]) -> None:
    assert set(_changes(bound[name])) == set(CHANGES) - set(NOT_APPLICABLE_ON[name])


#: Every change template on every model that binds it, as ``(model, template id)``.
CHANGE_RUNS = [(name, template_id) for name in MODELS for template_id in CHANGES if name in _applicable(template_id)]


@pytest.mark.parametrize(("name", "template_id"), CHANGE_RUNS)
def test_a_right_run_passes_the_checker_and_the_untouched_copy_fails_it(
    name: str, template_id: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    model = request.getfixturevalue(name)
    rendered = bound[name][template_id]
    task = _task(template_id, rendered)
    run = BIND.apply_right_run(model, rendered, tmp_path / "run")
    assert _codes(run, task, model.project) == []
    untouched = BIND.scratch_project(model, tmp_path / "untouched")
    assert "assertion" in _codes(untouched, task, model.project)


@pytest.mark.parametrize(("name", "template_id"), CHANGE_RUNS)
def test_a_right_run_that_also_rewrites_a_description_fails_outside_the_task(
    name: str, template_id: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    model = request.getfixturevalue(name)
    rendered = bound[name][template_id]
    run = BIND.apply_right_run(model, rendered, tmp_path / "run")
    # A bystander neither the prompt nor an assertion names; on the specimen, 24's links name every
    # component, hence the interface fallback.
    named = set(rendered.values.values()) | {a.get("name") for a in rendered.expect["assertions"]}
    layer, bystander = next(
        (layer, name) for layer in ("swc", "interfaces") for name in model.names(layer) if name not in named
    )
    element = model.by_name(layer)[bystander]
    tidied = element.model_copy(update={"description": (element.description or "") + " Tidied."})
    BIND.write_element(CHECK.model_root(run), layer, tidied, model.registry)
    assert _codes(run, _task(template_id, rendered), model.project) == ["outside-the-task"]


@pytest.mark.parametrize("template_id", CHANGES)
def test_a_right_run_writes_each_file_as_the_products_writers_do(
    template_id: str, showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    run = BIND.apply_right_run(showcase, bound["showcase"][template_id], tmp_path / "run")
    after = BIND.BoundModel.load(run / "swcraft.yaml")
    written = 0
    for layer in after.state.layer_names:
        elements = after.by_name(layer)
        for identity, files in stored_element_files(run / "model", layer, registry=after.registry).items():
            for layer_file in files:
                original = showcase.model_root / layer_file.path.relative_to(run / "model")
                text = layer_file.path.read_text(encoding="utf-8")
                if original.is_file() and original.read_text(encoding="utf-8") == text:
                    continue
                element = elements[identity]
                # The text reads back as the element: a product file, not the binder's echo.
                assert type(element).model_validate(load_yaml_document(text)) == element, (layer, identity)
                if layer == "requirements":
                    assert text == requirements_import._serialize(element), identity
                else:
                    assert text == BIND.element_text(element), (layer, identity)
                written += 1
    assert written >= 1


def _substituted(value: Any, mapping: dict[str, str]) -> Any:
    """*value* with every string the committed task names replaced by the bound one, ``ID@N`` included."""
    if isinstance(value, dict):
        return {key: _substituted(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_substituted(item, mapping) for item in value]
    if isinstance(value, str):
        head, at, version = value.partition("@")
        if value in mapping:
            return mapping[value]
        if at and head in mapping:
            return f"{mapping[head]}@{version}"
    return value


def _moves(before: dict[str, Any], after: dict[str, Any], names: dict[str, str]) -> list[tuple[str, str, str]]:
    """Each element *after* moves against *before*: its layer, its name, its fields.

    A created element's name is ``new``; any other's is mapped by *names*.
    """
    moved = []
    for layer in sorted(set(before) | set(after), key=str):
        for identity in sorted(set(before.get(layer, {})) | set(after.get(layer, {})), key=str):
            fields = CHECK._moved_fields(before.get(layer, {}).get(identity), after.get(layer, {}).get(identity))
            if fields:
                label = "new" if identity not in before.get(layer, {}) else names.get(identity, identity)
                moved.append((layer, label, ",".join(sorted(fields, key=str))))
    return sorted(moved)


@pytest.mark.parametrize("template_id", sorted(COMMITTED_VALUES, key=str))
def test_a_right_run_on_the_showcase_is_the_committed_task_with_the_bound_names(
    template_id: str, showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    """The committed assertions with the bound names are the rendered ones.

    The right run moves what the committed expected state moves.

    One difference is the binder's own: 03's application-type assertion also takes the by-reference
    form, where the committed task's takes the inline form alone.
    """
    rendered = bound["showcase"][template_id]
    committed = COMMITTED_VALUES[template_id]
    bound_values = {**rendered.values, "title": BIND.BENCH_REQUIREMENT_TITLE}
    mapping = {committed[key]: bound_values[key] for key in committed}
    assert any(committed[key] != rendered.values[key] for key in rendered.values)
    expect = _substituted(_committed(template_id)["expect"], mapping)
    if template_id == "03-new-interface-and-type":
        inline = {
            "layer": "application_types",
            "name": rendered.values["app_type"],
            "has": {"range": BIND.PAD_WEAR_RANGE},
        }
        assert expect["assertions"][0] == inline
        expect["assertions"][:1] = BIND._pad_wear_type(rendered.values["app_type"])
    assert expect == rendered.expect
    baseline = CHECK.read_model(AGENT_TASK_BENCH)
    overlay = CHECK.expected_model(AGENT_TASK_BENCH, template_id)
    run = CHECK.read_model(BIND.apply_right_run(showcase, rendered, tmp_path / "run"))
    assert _moves(baseline, run, {}) == _moves(baseline, overlay, mapping)


def _03_by_reference(model: Any, rendered: Any, run: Path, limits: dict[str, Any], unit: bool) -> Path:
    """03's right run restated in the model's by-reference form: a new constraint (and unit) the type names."""
    root = CHECK.model_root(run)
    after = BIND.BoundModel.load(run / "swcraft.yaml")
    app_type = after.by_name("application_types")[rendered.values["app_type"]]
    update: dict[str, Any] = {"range": None, "data_constr_ref": "BenchPadWear_Constr"}
    BIND.write_element(
        root,
        "data_constraints",
        DataConstr.model_validate({"name": "BenchPadWear_Constr", **limits, "level": 0}),
        after.registry,
    )
    if unit:
        BIND.write_element(
            root, "units", Unit.model_validate({"name": "BenchPercent", "display_name": "%"}), after.registry
        )
        update.update({"unit": "", "unit_ref": "BenchPercent"})
    BIND.write_element(root, "application_types", app_type.model_copy(update=update), after.registry)
    return run


@pytest.mark.parametrize("unit", [False, True], ids=["inline-unit", "unit-by-reference"])
@pytest.mark.parametrize("name", _applicable("03-new-interface-and-type"))
def test_03_passes_a_right_run_that_states_the_range_by_reference(
    name: str, unit: bool, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """A right run stating the range as ``data_constr_ref`` and the unit as ``unit_ref`` passes too."""
    model = request.getfixturevalue(name)
    rendered = bound[name]["03-new-interface-and-type"]
    run = _03_by_reference(
        model, rendered, BIND.apply_right_run(model, rendered, tmp_path / "run"), BIND.PAD_WEAR_RANGE, unit
    )
    after = CHECK.read_model(run)["application_types"][rendered.values["app_type"]]
    assert "range" not in after and after["data_constr_ref"] == "BenchPadWear_Constr"
    assert _codes(run, _task("03-new-interface-and-type", rendered), model.project) == []


def test_03_by_reference_holds_the_constraint_to_the_prompts_limits(
    showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    rendered = bound["showcase"]["03-new-interface-and-type"]
    task = _task("03-new-interface-and-type", rendered)
    wrong = _03_by_reference(
        showcase, rendered, BIND.apply_right_run(showcase, rendered, tmp_path / "wrong"), {"min": 0, "max": 255}, False
    )
    failures = CHECK.check_task_coded(wrong, task, baseline=showcase.project)
    assert [failure.code for failure in failures] == ["assertion"]
    assert "which does not contain" in failures[0].text


def test_03_admits_no_constraint_or_unit_the_type_does_not_name(
    showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    """The by-reference allowance admits the element the field names, never a stray one beside it."""
    rendered = bound["showcase"]["03-new-interface-and-type"]
    run = BIND.apply_right_run(showcase, rendered, tmp_path / "run")
    root = CHECK.model_root(run)
    BIND.write_element(
        root,
        "data_constraints",
        DataConstr.model_validate({"name": "Stray_Constr", "min": 0, "max": 100}),
        showcase.registry,
    )
    BIND.write_element(root, "units", Unit.model_validate({"name": "StrayUnit"}), showcase.registry)
    failures = CHECK.check_task_coded(run, _task("03-new-interface-and-type", rendered), baseline=showcase.project)
    assert [failure.code for failure in failures] == ["outside-the-task"]
    assert "data_constraints Stray_Constr: *" in failures[0].text and "units StrayUnit: *" in failures[0].text


def _03_by_compu_method(model: Any, rendered: Any, run: Path, referred: bool) -> Path:
    """03's right run in the import's conversion form: a new linear compu method naming a new unit.

    With *referred* the type names the method by ``compu_method_ref``; without, it names neither.
    """
    root = CHECK.model_root(run)
    after = BIND.BoundModel.load(run / "swcraft.yaml")
    BIND.write_element(
        root, "units", Unit.model_validate({"name": "BenchPercent", "display_name": "%"}), after.registry
    )
    BIND.write_element(
        root,
        "compu_methods",
        Linear.model_validate(
            {"name": "BenchPadWear_Linear", "kind": "linear", "unit": "BenchPercent", "factor": 1, "offset": 0}
        ),
        after.registry,
    )
    if referred:
        app_type = after.by_name("application_types")[rendered.values["app_type"]]
        update = {"unit": "", "compu_method_ref": "BenchPadWear_Linear"}
        BIND.write_element(root, "application_types", app_type.model_copy(update=update), after.registry)
    return run


@pytest.mark.parametrize("name", _applicable("03-new-interface-and-type"))
def test_03_passes_a_right_run_that_names_a_new_compu_method_and_its_unit(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """A right run naming a new linear compu method, its unit on the method, passes too."""
    model = request.getfixturevalue(name)
    rendered = bound[name]["03-new-interface-and-type"]
    run = _03_by_compu_method(model, rendered, BIND.apply_right_run(model, rendered, tmp_path / "run"), True)
    after = CHECK.read_model(run)["application_types"][rendered.values["app_type"]]
    assert after["compu_method_ref"] == "BenchPadWear_Linear" and "unit_ref" not in after
    assert _codes(run, _task("03-new-interface-and-type", rendered), model.project) == []


def test_03_admits_no_compu_method_the_type_does_not_name(
    showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    """A new compu method nothing refers to, and the unit only it names, stay outside the task."""
    rendered = bound["showcase"]["03-new-interface-and-type"]
    run = _03_by_compu_method(showcase, rendered, BIND.apply_right_run(showcase, rendered, tmp_path / "run"), False)
    failures = CHECK.check_task_coded(run, _task("03-new-interface-and-type", rendered), baseline=showcase.project)
    assert [failure.code for failure in failures] == ["outside-the-task"]
    assert "compu_methods BenchPadWear_Linear: *" in failures[0].text
    assert "units BenchPercent: *" in failures[0].text


def _data_type_mappings_assertions(rendered: Any) -> list[dict[str, Any]]:
    return [
        assertion for assertion in rendered.expect["assertions"] if "data_type_mappings" in assertion.get("has", {})
    ]


def test_03_adds_no_mapping_set_to_a_component_that_lists_its_set(
    showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    """On the showcase the component already lists the set: nothing is added or asserted, and the run passes."""
    rendered = bound["showcase"]["03-new-interface-and-type"]
    component, mapping_set = rendered.values["component"], rendered.values["mapping_set"]
    before = list(showcase.by_name("swc")[component].data_type_mappings)
    assert mapping_set in before
    run = BIND.apply_right_run(showcase, rendered, tmp_path / "run")
    assert CHECK.read_model(run)["swc"][component]["data_type_mappings"] == before
    assert _data_type_mappings_assertions(rendered) == []
    assert _codes(run, _task("03-new-interface-and-type", rendered), showcase.project) == []


def test_03_lists_the_mapping_set_on_a_component_that_lists_none(minimal: Any, tmp_path: Path) -> None:
    """On a specimen listing no mapping set, the set is added and asserted; without it ``check-model`` refuses."""
    rendered = BIND.new_interface_and_type(minimal)
    assert isinstance(rendered, BIND.Rendered)
    component, mapping_set = rendered.values["component"], rendered.values["mapping_set"]
    assert mapping_set in minimal.by_name("mapping_sets")
    assert not set(minimal.by_name("swc")[component].data_type_mappings) & set(minimal.by_name("mapping_sets"))
    run = BIND.apply_right_run(minimal, rendered, tmp_path / "run")
    assert CHECK.read_model(run)["swc"][component]["data_type_mappings"] == [mapping_set]
    assert _data_type_mappings_assertions(rendered) == [
        {"layer": "swc", "name": component, "has": {"data_type_mappings": [mapping_set]}}
    ]
    task = _task("03-new-interface-and-type", rendered)
    assert _codes(run, task, minimal.project) == []
    unlisted = BIND.apply_right_run(minimal, rendered, tmp_path / "unlisted")
    after = BIND.BoundModel.load(unlisted / "swcraft.yaml").by_name("swc")[component]
    BIND.write_element(
        CHECK.model_root(unlisted), "swc", after.model_copy(update={"data_type_mappings": []}), minimal.registry
    )
    assert "check-model" in _codes(unlisted, task, minimal.project)


def test_23_on_the_showcase_keeps_the_ports_links_and_adds_the_latest_version(
    showcase: Any, bound: dict[str, dict[str, Any]]
) -> None:
    rendered = bound["showcase"]["23-link-a-port-to-a-requirement"]
    assert (
        rendered.values["requirement"]
        == bound["showcase"]["18-elements-satisfying-a-requirement"].values["requirement"]
    )
    port = next(
        port
        for port in showcase.by_name("swc")[rendered.values["component"]].ports
        if port.name == rendered.values["port"]
    )
    assert port.direction == "provide"
    link = f"{rendered.values['requirement']}@{rendered.values['version']}"
    (assertion,) = rendered.expect["assertions"]
    assert assertion["has"]["ports"] == [{"name": port.name, "satisfies": [*port.satisfies, link]}]
    versions = [r.version for r in showcase.state["requirements"] if r.id == rendered.values["requirement"]]
    assert int(rendered.values["version"]) == max(versions)


def test_01_takes_the_first_interface_whose_right_run_the_checker_accepts(
    showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    """The first interface whose right run the checker accepts is bound.

    The showcase's most-named interface carries a QM element the B(D) requirer cannot consume without a
    measure: that edit fails ``check-model``, so the next interface is bound.
    """
    rendered = bound["showcase"]["01-connect-two-components"]
    values = rendered.values
    ranked = showcase.locator.heaviest_sender_receiver_interfaces()
    assert 0 < ranked.index(values["interface"]) < BIND.CONNECT_TRIES
    assert values["provider"] == showcase.locator.largest_component()
    run = BIND.apply_right_run(showcase, rendered, tmp_path / "run")
    after = BIND.BoundModel.load(run / "swcraft.yaml")
    for component, port in (
        (values["provider"], values["provider_port"]),
        (values["requirer"], values["requirer_port"]),
    ):
        element = after.by_name("swc")[component]
        ports = [
            held.model_copy(update={"interface": ranked[0]}) if held.name == port else held for held in element.ports
        ]
        BIND.write_element(run / "model", "swc", element.model_copy(update={"ports": ports}), after.registry)
    task = _task("01-connect-two-components", rendered)
    task["expect"] = json.loads(json.dumps(task["expect"]).replace(f'"{values["interface"]}"', f'"{ranked[0]}"'))
    assert _codes(run, task, showcase.project) == ["check-model"]


def test_a_new_name_takes_the_first_free_numeric_suffix() -> None:
    assert BIND._free_name("pBenchForwarded", ["pOther"]) == "pBenchForwarded"
    assert BIND._free_name("pBenchForwarded", ["pbenchforwarded", "pBenchForwarded2"]) == "pBenchForwarded3"
    assert BIND._free_name("SWR-BENCH-", [], width=3) == "SWR-BENCH-001"
    assert BIND._free_name("SWR-BENCH-", ["SWR-BENCH-001", "swr-bench-002"], width=3) == "SWR-BENCH-003"


def test_a_requirement_id_already_held_moves_07_to_the_next_free_one(showcase: Any, tmp_path: Path) -> None:
    project = BIND.scratch_project(showcase, tmp_path / "project")
    held = BIND.Requirement.model_validate(
        {"id": "SWR-BENCH-001", "title": "Held", "description": "Already here.", "version": 1, "source": "architecture"}
    )
    BIND.write_element(project / "model", "requirements", held, showcase.registry)
    rendered = BIND.bind(BIND.BoundModel.load(project / "swcraft.yaml"), _templates()["07-add-requirement"])
    assert rendered.values["requirement"] == "SWR-BENCH-002"


def test_a_right_run_the_checker_refuses_makes_the_template_not_applicable(
    showcase: Any, bound: dict[str, dict[str, Any]]
) -> None:
    rendered = bound["showcase"]["07-add-requirement"]
    idle = BIND.Rendered(
        values=rendered.values, expect=rendered.expect, reference=rendered.reference, right_run=lambda root: None
    )
    assert BIND.offer(showcase, idle) == BIND.NotApplicable("right-run-refused")
    assert BIND.offer(showcase, rendered) is rendered


@pytest.mark.parametrize("raised", [ValueError, KeyError, OSError])
def test_a_right_run_that_raises_makes_the_template_not_applicable_and_names_nothing(
    raised: type[Exception], showcase: Any, bound: dict[str, dict[str, Any]]
) -> None:
    """The product's importer refusing an export, a writer or a schema refusing a shape: one row, never the run."""
    rendered = bound["showcase"]["07-add-requirement"]

    def raising(root: Path) -> None:
        raise raised("ZqSentinelName")

    broken = BIND.Rendered(
        values=rendered.values, expect=rendered.expect, reference=rendered.reference, right_run=raising
    )
    failures = BIND.right_run_failures(showcase, broken)
    assert [failure.code for failure in failures] == [BIND.RIGHT_RUN_RAISED]
    assert "ZqSentinelName" not in failures[0].text
    assert BIND.offer(showcase, broken) == BIND.NotApplicable("right-run-refused")


def test_10_whose_import_conflicts_is_right_run_refused(
    showcase: Any, bound: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Benchmark task 10's right run calls the product's importer in-process; a conflict it raises costs the row."""
    from swcraft_tools.component_exchange import ComponentImportConflictError

    def conflicting(**_arguments: Any) -> None:
        raise ComponentImportConflictError("ZqSentinelName", ["ZqSentinelName"])

    monkeypatch.setattr(BIND, "import_component", conflicting)
    assert BIND.offer(showcase, bound["showcase"]["10-import-component"]) == BIND.NotApplicable("right-run-refused")


def test_a_baseline_with_an_error_makes_every_change_template_not_applicable(showcase: Any, tmp_path: Path) -> None:
    """On a copy with a ``check-model`` error every change template is ``baseline-has-errors``; a question binds."""
    project = BIND.scratch_project(showcase, tmp_path / "project")
    broken = BIND.Connector.model_validate(
        {
            "name": "BenchBroken",
            "from": {"swc": "NvM", "port": "pNoSuchPort"},
            "to": {"swc": "NvM", "port": "rNoSuchPort"},
        }
    )
    BIND.write_element(project / "model", "connectors", broken, showcase.registry)
    model = BIND.BoundModel.load(project / "swcraft.yaml")
    assert model.baseline_error_count > 0
    templates = _templates()
    for template_id in CHANGES:
        assert BIND.bind(model, templates[template_id]) == BIND.NotApplicable("baseline-has-errors"), template_id
    assert isinstance(BIND.bind(model, templates["11-ports-of-a-component"]), BIND.Rendered)


def test_the_showcases_baseline_has_no_error_and_a_scratch_copy_drops_the_toolchain_pin(
    showcase: Any, tmp_path: Path
) -> None:
    assert showcase.baseline_error_count == 0
    pinned = tmp_path / "pinned"
    shutil.copytree(showcase.project, pinned)
    manifest = pinned / "swcraft.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + 'toolchain_version: "0.0.1"\n', encoding="utf-8", newline="\n"
    )
    copy = BIND.scratch_project(BIND.BoundModel.load(manifest), tmp_path / "copy")
    text = (copy / "swcraft.yaml").read_text(encoding="utf-8")
    assert "toolchain_version" not in text and "model_root: model" in text
    assert BIND.check_model_error_count(copy) == 0


# ---------------------------------------------------------------------------
# The change templates that move, remove or rename (04, 05, 06, 24).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_id", ["04-move-runnable", "24-confirm-links-at-a-new-version"])
def test_where_a_moving_template_binds_the_committed_target_it_is_the_committed_task(
    template_id: str, showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    """The prompt is the committed one, and the right run moves what the committed expected state moves."""
    rendered = bound["showcase"][template_id]
    assert _task(template_id, rendered)["prompt"] == _committed(template_id)["prompt"]
    baseline = CHECK.read_model(AGENT_TASK_BENCH)
    overlay = CHECK.expected_model(AGENT_TASK_BENCH, template_id)
    run = CHECK.read_model(BIND.apply_right_run(showcase, rendered, tmp_path / "run"))
    assert _moves(baseline, run, {}) == _moves(baseline, overlay, {})


def test_24_on_the_showcase_asserts_the_committed_links(bound: dict[str, dict[str, Any]]) -> None:
    rendered = bound["showcase"]["24-confirm-links-at-a-new-version"]
    committed = _committed("24-confirm-links-at-a-new-version")["expect"]
    key = lambda assertion: json.dumps(assertion, sort_keys=True)  # noqa: E731
    assert sorted(map(key, rendered.expect["assertions"])) == sorted(map(key, committed["assertions"]))


@pytest.mark.parametrize("name", MODELS)
def test_04_moves_an_entry_no_sequence_names_between_tasks_of_one_partition_and_period(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest
) -> None:
    model = request.getfixturevalue(name)
    values = bound[name]["04-move-runnable"].values
    tasks = model.by_name("tasks")
    source, target = tasks[values["from_task"]], tasks[values["to_task"]]
    assert (source.partition, source.period) == (target.partition, target.period)
    steps = [step for sequence in model.state["sequences"] for step in sequence.steps]
    assert not any(
        step.runnable == values["runnable"] and values["component"] in (step.swc, step.instance) for step in steps
    )


def _old_04_pick(model: Any) -> tuple[Any, Any, Any] | None:
    """04's pick without the coexistence condition: the first movable entry of one partition and period."""
    component_of = BIND._resolver(model)
    steps = [step for sequence in model.state["sequences"] for step in sequence.steps]
    partitions: dict[str, list[Any]] = {}
    for _, task in sorted(model.by_name("tasks").items(), key=lambda item: item[0]):
        partitions.setdefault(task.partition, []).append(task)
    for partition in sorted(partitions, key=str):
        for source in partitions[partition]:
            for target in partitions[partition]:
                if target.name == source.name or target.period != source.period:
                    continue
                held = [BIND._entry(entry) for entry in target.runnables]
                for entry in source.runnables:
                    sequenced = any(
                        step.runnable == entry.runnable and component_of(step) == component_of(entry) for step in steps
                    )
                    if BIND._entry(entry) not in held and not sequenced:
                        return source, target, entry
    return None


def test_04_takes_a_pair_whose_move_needs_no_coexistence_argument(tmp_path: Path) -> None:
    """On ``passport-small`` the pick with the coexistence condition binds where the pick without it is refused.

    The pick without the condition is a move ``safety.task-mixed-asil`` refuses.
    """
    model = BIND.BoundModel.load(_specimen(tmp_path, "passport-small"))
    old = _old_04_pick(model)
    assert old is not None
    source, target, entry = old
    levels = BIND.effective_asil_levels(list(model.state["swc"]), list(model.state["requirements"]))
    assert not BIND._takes(target, entry, BIND._resolver(model), levels)
    assert BIND._moving(model, source, target, entry) == BIND.NotApplicable("right-run-refused")
    rendered = BIND.bind(model, _templates()["04-move-runnable"])
    assert isinstance(rendered, BIND.Rendered)
    assert (rendered.values["from_task"], rendered.values["to_task"]) != (source.name, target.name)
    run = BIND.apply_right_run(model, rendered, tmp_path / "run")
    assert BIND.check_model_error_count(run) == 0
    # The move without the condition, written: check-model's rule fires.
    old_run = BIND.scratch_project(model, tmp_path / "old-run")
    kept = [held for held in source.runnables if BIND._entry(held) != BIND._entry(entry)]
    BIND.write_element(old_run / "model", "tasks", source.model_copy(update={"runnables": kept}), model.registry)
    moved = target.model_copy(update={"runnables": [*target.runnables, entry]})
    BIND.write_element(old_run / "model", "tasks", moved, model.registry)
    completed = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "check-model", "--project", (old_run / "swcraft.yaml").as_posix()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode != 0 and "\n1 error(s)," in "\n" + completed.stderr
    assert f"Task '{target.name}' schedules components of 2 different ASILs and declares no coexistence argument" in (
        completed.stderr
    )


def test_07_takes_the_largest_component_that_has_a_require_port_ties_by_name(minimal: Any, tmp_path: Path) -> None:
    """On the minimal specimen, whose largest component provides only, 07 takes the largest with a require port."""
    components = minimal.by_name("swc")
    largest = minimal.locator.largest_component()
    assert all(port.direction == "provide" for port in components[largest].ports)
    rendered = BIND.bind(minimal, _templates()["07-add-requirement"])
    requirer = rendered.values["component"]
    require_ports = [port.name for port in components[requirer].ports if port.direction == "require"]
    assert requirer != largest and rendered.values["port"] == require_ports[0]
    project = BIND.scratch_project(minimal, tmp_path / "tie")
    twin = _renamed_copy(components[requirer], "A" + requirer)
    BIND.write_element(project / "model", "swc", twin, minimal.registry, {"group": "specimen"})
    tied = BIND.BoundModel.load(project / "swcraft.yaml")
    assert tied.locator.port_count(twin.name) == tied.locator.port_count(requirer)
    assert BIND.bind(tied, _templates()["07-add-requirement"]).values["component"] == twin.name


def _renamed_copy(component: Any, name: str) -> Any:
    """*component* under *name*, its ports linking no requirement and its runnables none."""
    ports = [port.model_copy(update={"satisfies": []}) for port in component.ports]
    return component.model_copy(update={"name": name, "ports": ports, "satisfies": [], "runnables": []})


@pytest.mark.parametrize("name", MODELS)
def test_05_removes_an_ordinary_component_and_every_connector_with_an_end_on_it(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    model = request.getfixturevalue(name)
    rendered = bound[name]["05-remove-component"]
    removed = rendered.values["component"]
    counts = sorted(len(component.ports) for component in model.state["swc"])
    assert len(model.by_name("swc")[removed].ports) <= 2 * BIND.statistics.median(counts)
    absent = {(a["layer"], a["name"]) for a in rendered.expect["assertions"] if a.get("absent")}
    ends = {
        ("connectors", name)
        for name, connector in model.by_name("connectors").items()
        if removed in (connector.from_.swc, connector.to.swc)
    }
    assert absent == {("swc", removed), *ends} and len(ends) == model.locator.connector_count(removed)
    after = BIND.BoundModel.load(BIND.apply_right_run(model, rendered, tmp_path / "run") / "swcraft.yaml")
    assert removed not in after.by_name("swc")
    assert all(member.type != removed for c in after.state["compositions"] for member in c.members)


def test_05_a_run_that_leaves_one_connector_fails_an_assertion(
    showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    rendered = bound["showcase"]["05-remove-component"]
    run = BIND.apply_right_run(showcase, rendered, tmp_path / "run")
    left = next(a["name"] for a in rendered.expect["assertions"] if a.get("layer") == "connectors")
    BIND.write_element(run / "model", "connectors", showcase.by_name("connectors")[left], showcase.registry)
    assert "assertion" in _codes(run, _task("05-remove-component", rendered), showcase.project)


@pytest.mark.parametrize("name", MODELS)
def test_06_states_every_access_to_the_element_on_a_port_of_the_interface(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest
) -> None:
    model = request.getfixturevalue(name)
    rendered = bound[name]["06-rename-data-element"]
    interface, old, new = (rendered.values[key] for key in ("interface", "element", "new_name"))
    expected = sorted(
        (component.name, runnable.name, field, ref.port)
        for component in model.state["swc"]
        for runnable in component.runnables
        for field in ("reads", "writes")
        for ref in getattr(runnable, field)
        if ref.element == old and any(port.name == ref.port and port.interface == interface for port in component.ports)
    )
    stated = sorted(
        (a["name"], runnable["name"], field, ref["port"])
        for a in rendered.expect["assertions"]
        if "has" in a and a["layer"] == "swc"
        for runnable in a["has"]["runnables"]
        for field in ("reads", "writes")
        for ref in runnable.get(field, [])
        if ref["element"] == new
    )
    assert stated == expected
    assert expected
    allowed = {a["name"] for a in rendered.expect["assertions"] if "may_change" in a}
    assert allowed == {component for component, _ in model.locator.ports_naming(interface)}


def test_06_a_run_that_renames_in_the_interface_only_fails(
    showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    rendered = bound["showcase"]["06-rename-data-element"]
    run = BIND.scratch_project(showcase, tmp_path / "run")
    interface = showcase.by_name("interfaces")[rendered.values["interface"]]
    elements = [
        element.model_copy(update={"name": rendered.values["new_name"]})
        if element.name == rendered.values["element"]
        else element
        for element in interface.data_elements
    ]
    BIND.write_element(
        run / "model", "interfaces", interface.model_copy(update={"data_elements": elements}), showcase.registry
    )
    codes = _codes(run, _task("06-rename-data-element", rendered), showcase.project)
    assert {"assertion", "check-model"} & set(codes)


def test_24_passes_over_a_requirement_with_a_stale_link(showcase: Any, tmp_path: Path) -> None:
    """A link left at an older version is not one to confirm: the requirement holding it is not 24's target."""
    project = BIND.scratch_project(showcase, tmp_path / "project")
    bound_before = BIND.bind(showcase, _templates()["24-confirm-links-at-a-new-version"])
    target = bound_before.values["requirement"]
    task = showcase.by_name("tasks")["Task_50ms"]
    stale = [f"{target}@0" if value.startswith(f"{target}@") else value for value in task.satisfies]
    BIND.write_element(project / "model", "tasks", task.model_copy(update={"satisfies": stale}), showcase.registry)
    model = BIND.BoundModel.load(project / "swcraft.yaml")
    links = BIND._requirement_links(model, target)
    assert any(
        value != f"{target}@{max(BIND.requirement_versions(model.state['requirements'])[target])}"
        for *_, value in links
    )
    rendered = BIND.confirm_links_at_a_new_version(model)
    assert not isinstance(rendered, BIND.Rendered) or rendered.values["requirement"] != target


# ---------------------------------------------------------------------------
# The change templates of the access forms (35, 36).
# ---------------------------------------------------------------------------

#: The committed tasks' targets, which the bindings pick on the showcase.
COMMITTED_35 = {
    "component": "BrakeController",
    "runnable": "ProcessBrakeRequest",
    "element": "temperature",
    "port": "rRearBrakeTemperature",
}
COMMITTED_36 = {
    "component": "DiagnosticManager",
    "runnable": "ProcessDiagnosticEvent",
    "operation": "WriteBlock",
    "port": "rNvStorage",
}
#: The committed 36's callback, the prompt's own name; the binder names it after the operation.
COMMITTED_CALLBACK = "OnSnapshotStored"


def _with_runnable(model: Any, component: str, runnable: str, **update: Any) -> Any:
    """*component* as *model* loads it, its runnable *runnable* updated by *update*."""
    element = model.by_name("swc")[component]
    runnables = [held.model_copy(update=update) if held.name == runnable else held for held in element.runnables]
    return element.model_copy(update={"runnables": runnables})


def test_35_on_the_showcase_is_the_committed_task(
    showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    """The assertions are the committed ones, and the right run moves what the committed expected state moves."""
    rendered = bound["showcase"]["35-make-a-read-implicit"]
    assert rendered.values == COMMITTED_35
    assert rendered.expect == _committed("35-make-a-read-implicit")["expect"]
    baseline = CHECK.read_model(AGENT_TASK_BENCH)
    overlay = CHECK.expected_model(AGENT_TASK_BENCH, "35-make-a-read-implicit")
    run = CHECK.read_model(BIND.apply_right_run(showcase, rendered, tmp_path / "run"))
    assert _moves(baseline, run, {}) == _moves(baseline, overlay, {})


def test_36_on_the_showcase_is_the_committed_task_with_its_callback_named_after_the_operation(
    showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    rendered = bound["showcase"]["36-call-asynchronously"]
    assert rendered.values == {**COMMITTED_36, "callback": "OnWriteBlockReturned"}
    committed = _committed("36-call-asynchronously")["expect"]
    assert _substituted(committed, {COMMITTED_CALLBACK: rendered.values["callback"]}) == rendered.expect
    baseline = CHECK.read_model(AGENT_TASK_BENCH)
    overlay = CHECK.expected_model(AGENT_TASK_BENCH, "36-call-asynchronously")
    run = CHECK.read_model(BIND.apply_right_run(showcase, rendered, tmp_path / "run"))
    assert _moves(baseline, run, {}) == _moves(baseline, overlay, {})


@pytest.mark.parametrize(
    ("target_form", "other_form"),
    [("value", None), (None, None), ("implicit", "implicit"), ("implicit", "value")],
    ids=["read-by-value", "read-left-by-argument", "other-read-made-implicit", "other-read-made-by-value"],
)
def test_35_a_run_that_leaves_the_read_or_moves_the_other_one_fails(
    target_form: str | None, other_form: str | None, showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    rendered = bound["showcase"]["35-make-a-read-implicit"]
    runnable = next(
        held for held in showcase.by_name("swc")["BrakeController"].runnables if held.name == "ProcessBrakeRequest"
    )
    reads = [
        read.model_copy(update={"by": target_form if read.port == "rRearBrakeTemperature" else other_form})
        for read in runnable.reads
    ]
    run = BIND.scratch_project(showcase, tmp_path / "run")
    BIND.write_element(
        CHECK.model_root(run),
        "swc",
        _with_runnable(showcase, "BrakeController", "ProcessBrakeRequest", reads=reads),
        showcase.registry,
    )
    assert "assertion" in _codes(run, _task("35-make-a-read-implicit", rendered), showcase.project)


def test_35_passes_over_a_read_the_implicit_form_would_break(showcase: Any, tmp_path: Path) -> None:
    """A read whose port states ``enable_update`` for the element is no candidate.

    The implicit form would break it (``swc.access.implicit-read-enable-update``), so the first admitted read
    in row order is taken.
    """
    project = BIND.scratch_project(showcase, tmp_path / "project")
    controller = showcase.by_name("swc")["BrakeController"]
    flagged = PortComSpec(data_element="temperature", enable_update=True)
    ports = [
        port.model_copy(update={"com_specs": [*port.com_specs, flagged]})
        if port.name == "rRearBrakeTemperature"
        else port
        for port in controller.ports
    ]
    BIND.write_element(project / "model", "swc", controller.model_copy(update={"ports": ports}), showcase.registry)
    model = BIND.BoundModel.load(project / "swcraft.yaml")
    assert model.baseline_error_count == 0
    rendered = BIND.make_a_read_implicit(model)
    assert isinstance(rendered, BIND.Rendered)
    assert rendered.values == {
        "component": "BrakeController",
        "runnable": "ProcessBrakeRequest",
        "element": "permittedAction",
        "port": "rPermittedBrakeAction",
    }
    (has, *lacks) = rendered.expect["assertions"]
    assert has["has"]["runnables"][0]["reads"] == [
        {"port": "rPermittedBrakeAction", "element": "permittedAction", "by": "implicit"},
        {"port": "rRearBrakeTemperature", "element": "temperature"},
    ]
    assert [lack["lacks"]["runnables"][0]["reads"][0]["by"] for lack in lacks] == ["value", "implicit"]


@pytest.mark.parametrize(
    ("asynchronous", "callback"),
    [(False, True), (True, False)],
    ids=["call-left-synchronous", "no-callback"],
)
def test_36_a_run_that_leaves_the_call_waiting_or_adds_no_callback_fails(
    asynchronous: bool, callback: bool, showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    rendered = bound["showcase"]["36-call-asynchronously"]
    run = BIND.apply_right_run(showcase, rendered, tmp_path / "run")
    after = BIND.BoundModel.load(run / "swcraft.yaml")
    component = after.by_name("swc")["DiagnosticManager"]
    runnables = [
        held.model_copy(
            update={"calls": [call.model_copy(update={"asynchronous": asynchronous}) for call in held.calls]}
        )
        if held.name == "ProcessDiagnosticEvent"
        else held
        for held in component.runnables
        if callback or held.name != rendered.values["callback"]
    ]
    BIND.write_element(
        CHECK.model_root(run), "swc", component.model_copy(update={"runnables": runnables}), after.registry
    )
    codes = _codes(run, _task("36-call-asynchronously", rendered), showcase.project)
    assert "assertion" in codes
    assert ("check-model" in codes) == (not asynchronous)


def test_36_passes_over_a_pair_its_component_calls_twice(showcase: Any, tmp_path: Path) -> None:
    """A pair two calls of one component name is no candidate (``swc.call.mixed-synchrony``)."""
    project = BIND.scratch_project(showcase, tmp_path / "project")
    manager = showcase.by_name("swc")["DiagnosticManager"]
    (call,) = next(held for held in manager.runnables if held.name == "ProcessDiagnosticEvent").calls
    second = call.model_copy(update={"name": None})
    BIND.write_element(
        project / "model",
        "swc",
        _with_runnable(showcase, "DiagnosticManager", "ServeSnapshot", calls=[second]),
        showcase.registry,
    )
    model = BIND.BoundModel.load(project / "swcraft.yaml")
    assert model.baseline_error_count == 0
    rendered = BIND.call_asynchronously(model)
    assert isinstance(rendered, BIND.Rendered)
    assert rendered.values == {
        "component": "InstrumentCluster",
        "runnable": "UpdateThermalStatus",
        "operation": "ReadSnapshot",
        "port": "rBrakeDiagnostics",
        "callback": "OnReadSnapshotReturned",
    }
    assert rendered.expect["assertions"][0]["has"]["runnables"][0]["calls"] == [
        {"port": "rBrakeDiagnostics", "operation": "ReadSnapshot", "asynchronous": True}
    ]


def test_36_names_its_callback_with_the_first_free_suffix(showcase: Any, tmp_path: Path) -> None:
    project = BIND.scratch_project(showcase, tmp_path / "project")
    manager = showcase.by_name("swc")["DiagnosticManager"]
    # Named like the callback, ignoring case.
    taken = type(manager.runnables[0]).model_validate({"name": "onwriteblockreturned"})
    BIND.write_element(
        project / "model",
        "swc",
        manager.model_copy(update={"runnables": [*manager.runnables, taken]}),
        showcase.registry,
    )
    rendered = BIND.call_asynchronously(BIND.BoundModel.load(project / "swcraft.yaml"))
    assert isinstance(rendered, BIND.Rendered)
    assert rendered.values["callback"] == "OnWriteBlockReturned2"


# ---------------------------------------------------------------------------
# The import template (10) and its generated export.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", MODELS)
def test_10_the_export_reads_back_through_swcraft_import_component(
    name: str, bound: dict[str, dict[str, Any]], request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    model = request.getfixturevalue(name)
    rendered = bound[name]["10-import-component"]
    ((relative, export),) = rendered.inputs.items()
    assert relative == f"imports/{rendered.values['file']}"
    project = BIND.scratch_project(model, tmp_path / "project")
    source = project / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(export)
    subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "import", "component",
         "--source", source.as_posix(), "--project", (project / "swcraft.yaml").as_posix()],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )  # fmt: skip
    imported = BIND.BoundModel.load(project / "swcraft.yaml")
    (assertion, *_) = rendered.expect["assertions"]
    component = imported.by_name("swc")[assertion["name"]]
    assert [(port.name, port.direction, port.interface) for port in component.ports] == [
        (port["name"], port["direction"], port["interface"]) for port in assertion["has"]["ports"]
    ]
    assert [runnable.name for runnable in component.runnables] == [BIND.IMPORT_RUNNABLE]
    assert component.memory == {}, "a component export carries no memory budget"
    assert {layer: imported.names(layer) for layer in imported.state.layer_names if layer != "swc"} == {
        layer: model.names(layer) for layer in model.state.layer_names if layer != "swc"
    }
    assert sorted(imported.names("swc"), key=str) == sorted([*model.names("swc"), assertion["name"]], key=str)


def test_10_on_the_showcase_has_the_committed_tasks_shape_with_the_bound_names(
    bound: dict[str, dict[str, Any]],
) -> None:
    rendered = bound["showcase"]["10-import-component"]
    committed = _committed("10-import-component")["expect"]
    assert rendered.expect["check_model"] == committed["check_model"]

    def shape(assertion: dict[str, Any]) -> tuple[str, list[str], list[str]]:
        has = assertion["has"]
        return assertion["layer"], sorted(has, key=str), sorted(has.get("memory", {}), key=str)

    assert [shape(a) for a in rendered.expect["assertions"]] == [shape(a) for a in committed["assertions"]]
    assert rendered.values["memory_clause"] == "It needs 512 bytes of RAM. "
    task = _task("10-import-component", rendered)
    assert task["prompt"].startswith(f"Import the component in imports/{rendered.values['file']}. It needs 512")


def test_10_states_the_first_ram_and_none_of_the_rest() -> None:
    assert BIND._memory_clause([]) == ""
    assert BIND._memory_clause(["RAM"]) == "It needs 512 bytes of RAM. "
    assert BIND._memory_clause(["RAM0", "RAM1"]) == "It needs 512 bytes of RAM0 and none of RAM1. "
    assert (
        BIND._memory_clause(["RAM0", "RAM1", "RAM2", "RAM3"])
        == "It needs 512 bytes of RAM0 and none of RAM1, RAM2 or RAM3. "
    )


def test_10_writes_its_export_into_the_scratch_copy_and_never_into_the_project(
    showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    rendered = bound["showcase"]["10-import-component"]
    run = BIND.apply_right_run(showcase, rendered, tmp_path / "run")
    ((relative, export),) = rendered.inputs.items()
    assert (run / relative).read_bytes() == export
    assert not (showcase.project / relative).exists(), "the committed bench's imports/ is left as it is"


def test_10_is_export_refused_where_the_product_refuses_the_export(
    showcase: Any, bound: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    """An element moved onto an external type stub, which the component export refuses to reach."""
    interface_name = bound["showcase"]["10-import-component"].expect["assertions"][0]["has"]["ports"][0]["interface"]
    project = BIND.scratch_project(showcase, tmp_path / "project")
    BIND.write_element(
        project / "model",
        "implementation_types",
        ExternalDataType.model_validate(
            {
                "name": "BenchStubType",
                "kind": "external_type",
                "external_ref": "/Elsewhere/BenchStubType",
                "external_dest": "IMPLEMENTATION-DATA-TYPE",
            }
        ),
        showcase.registry,
    )
    interface = showcase.by_name("interfaces")[interface_name]
    (first, *rest) = interface.data_elements
    moved = first.model_copy(update={"application_type": None, "implementation_type": "BenchStubType"})
    BIND.write_element(
        project / "model",
        "interfaces",
        interface.model_copy(update={"data_elements": [moved, *rest]}),
        showcase.registry,
    )
    model = BIND.BoundModel.load(project / "swcraft.yaml")
    assert BIND.import_a_component(model) == BIND.NotApplicable("export-refused")
    with pytest.raises(BIND.ExportRefused):
        BIND.write_component_export(
            showcase.manifest,
            "name: BenchDangling\nports:\n- name: rDangling\n  direction: require\n  interface: INoSuchInterface\n",
            "swc/application",
            "BenchDangling",
        )


def test_20_longer_than_the_head_is_not_applicable(
    showcase: Any, bound: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No one read orders 20's requirements and owners together, so a long 20 has no head to expect."""
    named = _answer(bound["showcase"]["20-links-needing-confirmation"])["mentions_all"]
    monkeypatch.setattr(BIND, "LONG_LIST_HEAD", len(named) - 1)
    template = _templates()["20-links-needing-confirmation"]
    assert BIND.bind(showcase, template) == BIND.NotApplicable("no-product-order")


def test_12_leaves_out_a_candidate_named_like_an_answers_instance(tmp_path: Path) -> None:
    """A component type named like an answer's instance is no negative candidate."""
    (tmp_path / "swcraft.yaml").write_text(agent_task_bench_module().MANIFEST, encoding="utf-8", newline="\n")
    model_root = tmp_path / "model"
    model_root.mkdir()
    registry = current_registry()
    interface = BIND.SRInterface.model_validate(
        {"name": "IStatus", "kind": "sender_receiver", "data_elements": [{"name": "value", "application_type": "T"}]}
    )
    BIND.write_element(model_root, "interfaces", interface, registry)
    ports = {"ProviderSwc": [{"name": "pStatus", "direction": "provide", "interface": "IStatus"}]}
    for name in ("ProviderSwc", "ExcludedSwc", "RootComposition", "OtherSwc"):
        component = BIND.SWComponent.model_validate({"name": name, "type": "application", "ports": ports.get(name, [])})
        BIND.write_element(model_root, "swc", component, registry, {"group": "app"})
    root = Composition.model_validate(
        {"name": "RootComposition", "title": "Root", "members": [{"name": "ExcludedSwc", "type": "ProviderSwc"}]}
    )
    BIND.write_element(model_root, "compositions", root, registry)
    model = BIND.BoundModel.load(tmp_path / "swcraft.yaml")
    assert model.instance_words(["ProviderSwc"]) == ["ExcludedSwc", "RootComposition"]
    assert model.instance_words(["OtherSwc"]) == []
    answer = _answer(BIND.bind(model, _templates()["12-components-on-an-interface"]))
    assert answer["mentions_all"] == ["ProviderSwc"]
    assert answer["mentions_none"] == ["OtherSwc"], "neither an instance name nor a composition name is refused"
    assert CHECK.check_answer("ProviderSwc (instance RootComposition/ExcludedSwc) uses IStatus.", answer) == []


def test_a_copied_model_root_that_is_a_repository_keeps_no_git_entry(tmp_path: Path) -> None:
    """A ``.git`` directory or file at any depth is not copied, so the bench commits files, never a gitlink."""
    project = tmp_path / "project"
    shutil.copytree(AGENT_TASK_BENCH, project)
    (project / "model" / ".git").mkdir()
    (project / "model" / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8", newline="\n")
    (project / "model" / "swc" / ".git").write_text("gitdir: ../../.git/modules/swc\n", encoding="utf-8", newline="\n")
    copy = tmp_path / "copy"
    BIND.copy_project(project / "swcraft.yaml", copy, keep_relative=True)
    assert not [path for path in copy.rglob(".git")], "no .git entry, file or directory, is copied"
    copied = sorted(
        path.relative_to(copy / "model").as_posix() for path in (copy / "model").rglob("*") if path.is_file()
    )
    original = sorted(
        path.relative_to(project / "model").as_posix()
        for path in (project / "model").rglob("*")
        if path.is_file() and ".git" not in path.relative_to(project / "model").parts
    )
    assert copied == original


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_a_copied_model_root_that_is_a_real_repository_is_committed_as_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(AGENT_TASK_BENCH, project)
    subprocess.run(["git", "init", "-q", str(project / "model")], check=True, capture_output=True)
    copy = tmp_path / "copy"
    BIND.copy_project(project / "swcraft.yaml", copy, keep_relative=True)
    assert not (copy / "model" / ".git").exists()
    subprocess.run(["git", "init", "-q", str(copy)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(copy), "add", "-f", "."], check=True, capture_output=True)
    staged = subprocess.run(
        ["git", "-C", str(copy), "ls-files", "-s", "model"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.splitlines()
    assert staged and not any(line.startswith("160000") for line in staged), "files, never a gitlink"


@pytest.mark.parametrize(
    ("field", "written", "copied"),
    [
        ("dist_root", "model", "dist"),
        ("dist_root", "Model/out", "dist"),
        ("views_root", "./model", "views"),
        ("outputs.arxml", "model/out.arxml", "dist/model.arxml"),
        ("outputs.needs", "model/needs.json", "dist/needs.json"),
        ("dist_root", "modelx", "modelx"),
    ],
)
def test_a_copys_output_root_that_meets_the_moved_model_root_moves_to_the_copys_own(
    field: str, written: str, copied: str, tmp_path: Path
) -> None:
    """``dist_root: model`` beside ``model_root: architecture`` moves to ``dist`` in the copy, which validates."""
    from swcraft_tools.project import require_project

    project = tmp_path / "project"
    project.mkdir()
    shutil.copytree(AGENT_TASK_BENCH / "model", project / "architecture")
    document = load_yaml_document((AGENT_TASK_BENCH / "swcraft.yaml").read_text(encoding="utf-8"))
    document["model_root"] = "architecture"
    key, _, sub = field.partition(".")
    if sub:
        document.setdefault("outputs", {})[sub] = written
    else:
        document[key] = written
    manifest = project / "swcraft.yaml"
    manifest.write_text(BIND.yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    require_project(manifest.as_posix())
    copy = tmp_path / "copy"
    BIND.copy_project(manifest, copy)
    result = load_yaml_document((copy / BIND.MANIFEST).read_text(encoding="utf-8"))
    assert result["model_root"] == BIND.SCRATCH_MODEL_ROOT
    assert (result["outputs"][sub] if sub else result[key]) == copied
    require_project((copy / BIND.MANIFEST).as_posix())

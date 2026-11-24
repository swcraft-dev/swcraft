"""The recipes the agents are given are run, and their answers are checked.

The SWCraft Architect profile (the template's and the extension's copy) and SWCraft's own agent prompt
carry one section, "Recipes for common questions"; the user guide's How-to pages also carry recipes, as
recipe blocks. The gate holds four things:

1. **One text.** The section is byte-equal in all three files and holds exactly its questions, in order.
2. **Every request runs.** Each fenced JSON block goes through the kernel the MCP tool calls, sent as
   the tool sends it (``read`` and ``find`` through the read engine, ``impact_of`` through
   :func:`swcraft_tools.impact.impact_of` and :func:`swcraft_tools.impact.count_impact`,
   ``resource_report`` through :mod:`swcraft_tools.resource_report`). A ``<NAME>`` placeholder is filled
   from :data:`agent_recipe_support.PLACEHOLDERS`; an unknown one fails.
3. **Every answer is the expected one.** The recipe's own step is applied and the result compared with the
   answer :mod:`agent_recipe_support` built the model to give, not merely with the absence of a refusal.
4. **One loader.** :func:`all_recipes` reads profile recipes and How-to recipe blocks as the same
   :class:`Recipe`, so a How-to recipe is run and checked like a profile one; a title twice, in one source or in
   both, is refused.

Does not reach: the MCP tools themselves (this module cannot import the server;
``packages/mcp/tests/test_vscode_agent_profile.py`` holds each recipe's argument names to the tool's
signature); what an LLM makes of a recipe, which only the manual question-set run in Copilot shows.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import agent_recipe_support as support
import pytest
from conftest import REPO_ROOT, TEMPLATE_ROOT
from swcraft_tools import impact, validator
from swcraft_tools import resource_report as resource_report_mod
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.project import require_project
from swcraft_tools.safety import ASIL_LEVELS
from swcraft_tools.snapshot import load_snapshot

#: The three files that carry the section.
TEMPLATE_PROFILE = TEMPLATE_ROOT / ".github" / "agents" / "swcraft-architect.agent.md"
BUNDLED_PROFILE = REPO_ROOT / "vscode_extension" / "fixtures" / "agents" / "swcraft-architect.agent.md"
AGENT_PROMPT = REPO_ROOT / "packages" / "agent" / "src" / "swcraft_agent" / "agent-system-prompt.md"
RECIPE_FILES = (TEMPLATE_PROFILE, BUNDLED_PROFILE, AGENT_PROMPT)

SECTION_HEADING = "## Recipes for common questions"

#: The questions the profile's section answers, in its order.
PROFILE_RECIPE_TITLES = (
    "The sender-receiver table",
    "Elements without a description",
    "Components by effective level",
    "How many components are at a level",
    "A component's partitions",
    "Connections that cross a partition",
    "Components that no partition runs",
)

#: The recipes the How-to pages hold as recipe blocks, in page order: the components and ports
#: page, the requirements page, the memory page, then the change page (the pages in path order).
HOW_TO_RECIPE_TITLES = (
    "Who reads or writes a data element",
    "Ports that nothing connects",
    "A component's unconnected ports",
    "Components a partition runs",
    "Tasks that run on a core",
    "Components with a port on an interface",
    "Who realises a requirement",
    "Requirements nothing realises, and stale links",
    "Requirements about a topic",
    "Elements that could realise a requirement",
    "Whether a memory holds a new component",
    "The largest consumer of a memory",
    "What a change affects",
)

#: Every recipe, in the one loader's order: the profile's, then the How-to pages'.
RECIPE_TITLES = PROFILE_RECIPE_TITLES + HOW_TO_RECIPE_TITLES

#: The page size of every list request, and the recipes that differ, each with its reason.
PAGE = 20
PAGE_OF = {"The sender-receiver table": 5}  # a row carries about thirteen identities
WHOLE_LISTS = {"Components that no partition runs"}  # a set difference needs both whole lists

TOOLS = ("read", "find", "impact_of", "resource_report")
_TOOL_LINE = re.compile(r"^Tool: `([a-z_]+)`", re.MULTILINE)
_JSON_BLOCK = re.compile(r"^```json\n(.*?)\n```$", re.MULTILINE | re.DOTALL)
#: A recipe request's placeholder string (``<COMPONENT>``); the functional harness fills by it too.
PLACEHOLDER = re.compile(r"<[A-Z_]+>")


@dataclass(frozen=True)
class Recipe:
    title: str
    tool: str
    requests: tuple[dict[str, Any], ...]
    #: The recipe's text under its heading, the requests included.
    body: str = ""


def recipe_section(text: str) -> str:
    """The section from its heading to the next ``## `` heading or ``---`` rule."""
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.rstrip("\n") == SECTION_HEADING]
    assert len(starts) == 1, f"expected one {SECTION_HEADING!r}, found {len(starts)}"
    body = [lines[starts[0]]]
    for line in lines[starts[0] + 1 :]:
        if line.startswith("## ") or line.rstrip("\n") == "---":
            break
        body.append(line)
    return "".join(body)


def parse_recipes(section: str) -> list[Recipe]:
    recipes = []
    for chunk in section.split("\n### ")[1:]:
        title, _, body = chunk.partition("\n")
        tools = _TOOL_LINE.findall(body)
        assert len(tools) == 1, f"{title!r} names its tool once (`Tool: ...`), found {tools}"
        requests = tuple(json.loads(block) for block in _JSON_BLOCK.findall(body))
        recipes.append(Recipe(title=title.strip(), tool=tools[0], requests=requests, body=body))
    return recipes


def fill(value: Any) -> Any:
    """*value* with every placeholder string replaced by the recipe model's name."""
    if isinstance(value, dict):
        return {key: fill(item) for key, item in value.items()}
    if isinstance(value, list):
        return [fill(item) for item in value]
    if isinstance(value, str) and PLACEHOLDER.fullmatch(value):
        assert value in support.PLACEHOLDERS, f"unknown placeholder {value}"
        return support.PLACEHOLDERS[value]
    return value


def _section_of(path: Path) -> str:
    return recipe_section(path.read_text(encoding="utf-8"))


#: The profile's own recipes, as its section holds them.
PROFILE_RECIPES = {recipe.title: recipe for recipe in parse_recipes(_section_of(TEMPLATE_PROFILE))}

#: The guide's How-to section, whose pages carry recipe blocks.
HOW_TO_DIR = REPO_ROOT / "docs" / "guide" / "how-to"

#: A recipe block's title comment on a How-to page: ``<!-- recipe: <title> -->``.
_RECIPE_COMMENT = re.compile(r"^<!-- recipe: (\S(?:.*\S)?) -->$", re.MULTILINE)

#: What ends a recipe block: the next heading.
_HEADING = re.compile(r"^#{1,6} ", re.MULTILINE)


def parse_how_to_recipes(text: str) -> list[Recipe]:
    """Each recipe block of a How-to page, as the same :class:`Recipe` a profile recipe is.

    A block runs from its ``<!-- recipe: <title> -->`` comment to the next recipe comment or heading; its
    requests are the ``arguments`` of its call blocks. A block whose calls name two tools, or none, is
    refused: a recipe is one tool's requests.
    """
    recipes = []
    comments = list(_RECIPE_COMMENT.finditer(text))
    for number, comment in enumerate(comments):
        end = comments[number + 1].start() if number + 1 < len(comments) else len(text)
        body = text[comment.end() : end]
        heading = _HEADING.search(body)
        if heading is not None:
            body = body[: heading.start()]
        calls = [json.loads(block) for block in _JSON_BLOCK.findall(body)]
        tools = sorted({call["tool"] for call in calls})
        assert len(tools) == 1, f"the recipe block {comment.group(1)!r} names one tool, found {tools}"
        recipes.append(
            Recipe(
                title=comment.group(1),
                tool=tools[0],
                requests=tuple(call["arguments"] for call in calls),
                body=body,
            )
        )
    return recipes


def how_to_recipes() -> list[Recipe]:
    """Every How-to page's recipe blocks, the pages in path order."""
    recipes: list[Recipe] = []
    for path in sorted(HOW_TO_DIR.glob("*.md"), key=lambda path: path.as_posix()):
        recipes.extend(parse_how_to_recipes(path.read_text(encoding="utf-8")))
    return recipes


def one_source_per_title(profile: list[Recipe], how_to: list[Recipe]) -> list[Recipe]:
    """*profile*'s recipes, then *how_to*'s; a title twice in one source or in both is refused."""
    profile_titles = [recipe.title for recipe in profile]
    how_to_titles = [recipe.title for recipe in how_to]
    for titles, where in ((profile_titles, "the profile"), (how_to_titles, "the How-to pages")):
        repeated = sorted({title for title in titles if titles.count(title) > 1})
        assert not repeated, f"a recipe title stands more than once in {where}: {repeated}"
    both = sorted(set(profile_titles) & set(how_to_titles))
    assert not both, f"a recipe has one source; in the profile and on a How-to page: {both}"
    return [*profile, *how_to]


def all_recipes() -> list[Recipe]:
    """The one loader: the profile's recipes, then the How-to pages' recipe blocks, every title once.

    Not reached: a call block outside a recipe block, a page's step.
    """
    return one_source_per_title(parse_recipes(_section_of(TEMPLATE_PROFILE)), how_to_recipes())


#: Every recipe by title, wherever it stands: what the gate below runs.
RECIPES = {recipe.title: recipe for recipe in all_recipes()}


@pytest.fixture(scope="module")
def recipe_project(tmp_path_factory: pytest.TempPathFactory) -> Any:
    manifest = support.build_recipe_model(tmp_path_factory.mktemp("recipes") / "project")
    return require_project(str(manifest))


def report_answer(project: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """The answer the MCP ``resource_report`` tool gives *arguments*: one question, or the overview.

    The tool's argument checks are the server's; a shape it refuses fails here on a missing key or wrong kind.
    """
    model_root = project.resolved_model_root
    report = resource_report_mod.resource_report(model_root, validator.check_hardware_topology(model_root).records)
    assert len(arguments) <= 1, arguments
    if "fits" in arguments:
        fits = arguments["fits"]
        if set(fits) == {"memory", "bytes"}:
            assert isinstance(fits["bytes"], int), fits
            return resource_report_mod.fits_answer(
                report, resource_report_mod.KIND_MEMORY, fits["memory"], Fraction(fits["bytes"])
            )
        assert set(fits) == {"core", "utilization"}, fits
        return resource_report_mod.fits_answer(
            report, resource_report_mod.KIND_CORE, fits["core"], Fraction(str(fits["utilization"]))
        )
    if "memory" in arguments:
        return resource_report_mod.resource_answer(report, resource_report_mod.KIND_MEMORY, arguments["memory"])
    if "core" in arguments:
        return resource_report_mod.resource_answer(report, resource_report_mod.KIND_CORE, arguments["core"])
    return resource_report_mod.overview(report)


def run(project: Any, recipe: Recipe) -> list[Any]:
    """Every request of *recipe*, filled and sent as its tool sends it."""
    answers = []
    for request in recipe.requests:
        arguments = fill(request)
        if recipe.tool == "read" and arguments.get("form") == "count":
            response = handle_request({"operation": "read"} | arguments, project=project)
            assert response.get("response") == "count", response
            answers.append(response["count"])
        elif recipe.tool == "read":
            response = handle_request({"operation": "read"} | arguments, project=project)
            assert response.get("response") == "read", response
            answers.append(response["value"]["identities"])
        elif recipe.tool == "find":
            response = handle_request({"operation": "find"} | arguments, project=project)
            assert response.get("response") == "find", response
            answers.append(response)
        elif recipe.tool == "impact_of":
            form = arguments.pop("form")
            snapshot = load_snapshot(project.resolved_model_root, rev=None)
            result = impact.impact_of(snapshot, **arguments)
            answers.append(impact.count_impact(result) if form == "count" else result)
        else:
            answers.append(report_answer(project, arguments))
    return answers


# Each recipe's step, applied to its answers.


def _accesses(rows: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [(row["element"], row["through"][0], row["member"]["port"]) for row in rows]


def step_readers_and_writers(answers: list[Any]) -> Any:
    readers, writers = answers
    return {"readers": _accesses(readers), "writers": _accesses(writers)}


def step_sender_receiver(answers: list[Any]) -> Any:
    (rows,) = answers
    return [
        (
            row["element"],
            row["through"][0],
            row["member"]["port"],
            row["member"]["element"],
            row["connection"]["element"],
            row["reader"]["element"],
            row["reader"]["through"][0],
            row["reader"]["member"]["port"],
        )
        for row in rows
    ]


def step_identities(answers: list[Any]) -> Any:
    (identities,) = answers
    return identities


def step_count_and_which(answers: list[Any]) -> Any:
    count, rows = answers
    return {"count": count, "which": [row["element"] for row in rows]}


def step_elements(answers: list[Any]) -> Any:
    (rows,) = answers
    return [row["element"] for row in rows]


def step_unconnected(answers: list[Any]) -> Any:
    """The count, then the page of ports it counts (the long-list rule: the number first)."""
    count, rows = answers
    return {"count": count, "ports": [(row["element"], row["member"]) for row in rows]}


def step_ports(answers: list[Any]) -> Any:
    (rows,) = answers
    return [(row["element"], row["member"]) for row in rows]


def step_tasks_on_a_core(answers: list[Any]) -> Any:
    (rows,) = answers
    return [(row["element"], row["derived"]["core"]["partition"]) for row in rows]


def step_partitions(answers: list[Any]) -> Any:
    (rows,) = answers
    return [(row["derived"]["partitions"]["partition"], row["derived"]["partitions"]["core"]) for row in rows]


def step_crossings(answers: list[Any]) -> Any:
    (rows,) = answers
    return [
        (
            row["element"],
            row["derived"]["crossing_ends"]["provider"]["swc"],
            row["derived"]["crossing_ends"]["provider"]["partition"],
            row["derived"]["crossing_ends"]["requester"]["swc"],
            row["derived"]["crossing_ends"]["requester"]["partition"],
        )
        for row in rows
    ]


def step_unplaced(answers: list[Any]) -> Any:
    every, placed = answers
    placed_names = {row["element"] for row in placed}
    return [name for name in every if name not in placed_names]


def step_realised_by(answers: list[Any]) -> Any:
    (rows,) = answers
    return [
        (site["layer"], site["element"], site["member"], site["kind"], site["stated"])
        for site in (row["derived"]["realised_by"] for row in rows)
    ]


def step_unallocated_and_stale(answers: list[Any]) -> Any:
    unallocated, stale = answers
    return {
        "unallocated": [row["element"] for row in unallocated],
        "stale": [
            (row["element"], link["layer"], link["element"], link["member"], link["stated"], link["current"])
            for row, link in ((row, row["derived"]["stale_links"]) for row in stale)
        ],
    }


def step_requirements_about(answers: list[Any]) -> Any:
    (answer,) = answers
    assert "more" not in answer, answer
    return [candidate["address"]["element"] for candidate in answer["candidates"]]


def step_could_realise(answers: list[Any]) -> Any:
    """The places in the server's order, and the terms the first one shares — the step explains, never re-ranks."""
    (answer,) = answers
    places = answer["places"]
    (first_text,) = [record for record in places[0]["evidence"] if record["kind"] == "text"]
    return {"places": [place["group"]["name"] for place in places], "first_terms": first_text["terms"]}


def step_impact(answers: list[Any]) -> Any:
    count, result = answers
    grouped: dict[str, list[str]] = {}
    for dependents in result.values():
        for dependent in dependents:
            grouped.setdefault(dependent.ref.layer, []).append(dependent.ref.name)
    return {"count": count, "affected": {layer: sorted(names) for layer, names in sorted(grouped.items())}}


def step_fits(answers: list[Any]) -> Any:
    (answer,) = answers
    assert answer["response"] == "fits", answer
    return answer["fits"]


def step_largest_consumer(answers: list[Any]) -> Any:
    (answer,) = answers
    assert answer["response"] == "resource", answer
    return answer["consumers"][0]["name"]


STEPS: dict[str, Callable[[list[Any]], Any]] = {
    "Who reads or writes a data element": step_readers_and_writers,
    "The sender-receiver table": step_sender_receiver,
    "Elements without a description": step_identities,
    "Components by effective level": step_count_and_which,
    "How many components are at a level": step_count_and_which,
    "A component's partitions": step_partitions,
    "Components a partition runs": step_elements,
    "Connections that cross a partition": step_crossings,
    "Components that no partition runs": step_unplaced,
    "Ports that nothing connects": step_unconnected,
    "A component's unconnected ports": step_unconnected,
    "Tasks that run on a core": step_tasks_on_a_core,
    "Components with a port on an interface": step_ports,
    "Who realises a requirement": step_realised_by,
    "Requirements nothing realises, and stale links": step_unallocated_and_stale,
    "Requirements about a topic": step_requirements_about,
    "Elements that could realise a requirement": step_could_realise,
    "What a change affects": step_impact,
    "Whether a memory holds a new component": step_fits,
    "The largest consumer of a memory": step_largest_consumer,
}


# The gate.


@pytest.mark.parametrize("path", RECIPE_FILES[1:], ids=lambda path: path.name)
def test_the_section_is_byte_equal_in_every_file(path: Path) -> None:
    assert _section_of(path) == _section_of(TEMPLATE_PROFILE)


def test_the_cli_command_the_section_gives_names_the_project_it_requires() -> None:
    """``swcraft query`` refuses a run without ``--project`` (argparse's usage error), so the command does too."""
    section = " ".join(_section_of(TEMPLATE_PROFILE).split())
    assert "`swcraft query --request FILE --project PATH`" in section
    assert "`swcraft query --request FILE`" not in section


def test_the_recipes_are_the_questions_in_their_order() -> None:
    assert tuple(PROFILE_RECIPES) == PROFILE_RECIPE_TITLES
    assert tuple(RECIPES) == RECIPE_TITLES
    assert set(STEPS) == set(RECIPE_TITLES) == set(support.EXPECTED)


def test_the_profile_holds_no_recipe_a_how_to_page_holds() -> None:
    """A recipe that moved into the guide left the profile: the index names its page instead."""
    assert not set(PROFILE_RECIPE_TITLES) & set(HOW_TO_RECIPE_TITLES)
    section = _section_of(TEMPLATE_PROFILE)
    for title in HOW_TO_RECIPE_TITLES:
        assert f"### {title}\n" not in section, title


def test_the_profile_s_guide_index_names_every_moved_recipe_once() -> None:
    """The loader's How-to titles and the profile's index lines are the same set.

    The line and its section are ``packages/tools/tests/test_guide_profile_index.py``'s.
    """
    text = TEMPLATE_PROFILE.read_text(encoding="utf-8")
    for title in HOW_TO_RECIPE_TITLES:
        assert text.count(f'\n  - {title} → `guide(section="how-to/') == 1, title


@pytest.mark.parametrize("title", RECIPE_TITLES)
def test_every_recipe_names_a_tool_and_carries_requests(title: str) -> None:
    recipe = RECIPES[title]
    assert recipe.tool in TOOLS
    assert recipe.requests, f"{title!r} carries no request"
    for request in recipe.requests:
        fill(request)  # every placeholder is one the model fills
        if recipe.tool == "impact_of":
            assert set(request) == {"name", "form", "direction", "depth"}, request
            assert request["form"] in ("count", "summary"), request
        elif recipe.tool == "resource_report":
            assert set(request) in ({"memory"}, {"core"}, {"fits"}), request
        elif recipe.tool == "find":
            assert set(request) in ({"text"}, {"for_requirement", "candidates"}), request
        else:
            assert request["form"] in ("index", "count"), request


def test_the_profile_keeps_reads_and_the_how_to_pages_hold_every_other_tool() -> None:
    """The profile's remaining recipes are all ``read``; the finds, the impact and the resources are the guide's."""
    assert [recipe.tool for recipe in PROFILE_RECIPES.values()] == ["read"] * len(PROFILE_RECIPE_TITLES)
    assert [RECIPES[title].tool for title in HOW_TO_RECIPE_TITLES] == ["read"] * 8 + [
        "find",
        "find",
        "resource_report",
        "resource_report",
        "impact_of",
    ]


def test_the_impact_recipe_counts_before_it_lists() -> None:
    """Rule 9: a question about impact starts with its count; the summary is the second request."""
    forms = [request["form"] for request in RECIPES["What a change affects"].requests]
    assert forms == ["count", "summary"]


def test_the_impact_count_counts_the_walk_it_lists(recipe_project: Any) -> None:
    """The count and the summary are one walk: its totals are the listed rows, per kind."""
    answer = STEPS["What a change affects"](run(recipe_project, RECIPES["What a change affects"]))
    (matched,) = answer["count"].values()
    assert matched["total"] == sum(len(names) for names in answer["affected"].values())
    assert matched["by_kind"] == {kind: len(names) for kind, names in answer["affected"].items()}


def test_the_fit_is_decided_by_the_budget_not_the_free_bytes(recipe_project: Any) -> None:
    """The fit is decided by the budget: a recipe answered off ``size - used`` would pass on its own amount.

    230 000 bytes fit ``RAM0``'s 257 536 raw free bytes but not its 205 107.2-byte headroom under the threshold.
    """
    request = fill(RECIPES["Whether a memory holds a new component"].requests[0])
    answer = report_answer(recipe_project, {"fits": request["fits"] | {"bytes": 230_000}})
    assert answer["size"] - int(answer["used"]) >= 230_000
    assert answer["fits"] is False
    assert answer["within_size"]["fits"] is True
    # The recipe's quoted sentence is the server's, its figure aside.
    prefix = resource_report_mod.ANSWER_WITHIN_SIZE_ONLY.partition("{excess}")[0]
    assert answer["answer"].startswith(prefix), answer["answer"]
    assert answer["answer"].endswith(" bytes"), answer["answer"]


#: The keys of the ``fits`` answer the fit recipe tells the agent to read.
FIT_RECIPE_KEYS = ("fits", "within_threshold", "within_size", "limit", "headroom", "answer")


def test_the_fit_recipe_names_the_keys_of_the_answer_it_reads(recipe_project: Any) -> None:
    """A recipe naming a key the answer lacks would send the agent to read nothing."""
    recipe = RECIPES["Whether a memory holds a new component"]
    answer = report_answer(recipe_project, fill(recipe.requests[0]))
    for key in FIT_RECIPE_KEYS:
        assert f"`{key}`" in recipe.body, key
    assert {"fits", "within_threshold", "within_size", "answer"} <= set(answer)
    for verdict in ("within_threshold", "within_size"):
        assert set(answer[verdict]) == {"fits", "limit", "headroom"}
    quoted = resource_report_mod.ANSWER_WITHIN_SIZE_ONLY.format(excess="N bytes")
    assert quoted in " ".join(recipe.body.split())


def test_the_largest_consumer_is_not_the_report_s_order(recipe_project: Any) -> None:
    """The consumers come largest first: ``ExampleConsumerSWC`` sorts first by name, ``ExampleSWC`` holds the most."""
    (answer,) = run(recipe_project, RECIPES["The largest consumer of a memory"])
    budgets = [row["bytes"] for row in answer["consumers"]]
    assert budgets == sorted(budgets, reverse=True)
    assert budgets[0] > budgets[1]


def test_the_recipe_model_is_a_valid_model(recipe_project: Any) -> None:
    result = validator.check_cross_layer_consistency(recipe_project.resolved_model_root)
    assert result.consistent, result.issues


def test_the_recipe_model_has_a_connector_that_crosses_nothing(recipe_project: Any) -> None:
    """Without one, a recipe that answered every connector would pass the crossing check."""
    response = handle_request(
        {"operation": "read", "address": {"layer": "connectors"}, "form": "index"}, project=recipe_project
    )
    assert "Example_to_Local" in response["value"]["identities"]
    assert all(crossing[0] != "Example_to_Local" for crossing in support.EXPECTED["Connections that cross a partition"])


@pytest.mark.parametrize("title", RECIPE_TITLES)
def test_every_recipe_gives_the_expected_answer(recipe_project: Any, title: str) -> None:
    answer = STEPS[title](run(recipe_project, RECIPES[title]))
    assert answer == support.EXPECTED[title]


def test_the_recipe_model_has_a_decomposed_level(recipe_project: Any) -> None:
    """Without one, a step comparing the level's spelling would pass the by-level check."""
    response = handle_request(
        {"operation": "read", "address": {"layer": "swc"}, "form": "index", "derived": ["effective_level"]},
        project=recipe_project,
    )
    levels = {row["element"]: row["derived"]["effective_level"] for row in response["value"]["identities"]}
    assert levels["UnplacedSWC"] == {"level": "B(D)", "rating": "B"}


def test_the_count_recipe_counts_the_rating_not_the_spelling(recipe_project: Any) -> None:
    """A clause on ``level`` would count the plain ``B`` alone; the recipe's ``rating`` counts ``B(D)`` too."""
    request = {
        "operation": "read",
        "address": {"layer": "swc"},
        "form": "count",
        "derived": ["effective_level"],
        "where": [{"field": "derived.effective_level.level", "equals": "B"}],
    }
    assert handle_request(request, project=recipe_project)["count"] == 1
    counted = fill(RECIPES["How many components are at a level"].requests[0])
    assert counted["where"] == [{"field": "derived.effective_level.rating", "equals": "B"}]
    assert handle_request({"operation": "read", **counted}, project=recipe_project)["count"] == 2


def test_every_list_request_carries_its_page() -> None:
    """Every ``index`` request of a ``read`` recipe carries its page.

    It pages at twenty, the table at five, and a set difference reads both whole lists.
    """
    assert set(PAGE_OF) <= set(RECIPE_TITLES)
    assert set(WHOLE_LISTS) <= set(RECIPE_TITLES)
    for recipe in RECIPES.values():
        if recipe.tool != "read":
            continue
        for request in recipe.requests:
            if request["form"] != "index":
                assert "limit" not in request, (recipe.title, request)
            elif recipe.title in WHOLE_LISTS:
                assert "limit" not in request, (recipe.title, request)
            else:
                assert request.get("limit") == PAGE_OF.get(recipe.title, PAGE), (recipe.title, request)


def test_the_by_level_recipe_counts_each_level_then_pages_one(recipe_project: Any) -> None:
    """Its step names every rating, and its requests are the at-a-level recipe's own two.

    The counts are checked against the independent grouping
    :data:`agent_recipe_support.EFFECTIVE_LEVELS_BY_RATING`.
    """
    recipe = RECIPES["Components by effective level"]
    (step,) = [paragraph for paragraph in recipe.body.split("\n\n") if paragraph.startswith("Step:")]
    for level in ASIL_LEVELS:
        assert f"`{level}`" in step, level
    assert recipe.requests == RECIPES["How many components are at a level"].requests
    counted = {}
    for level in ASIL_LEVELS:
        request = json.loads(json.dumps(recipe.requests[0]).replace('"<LEVEL>"', json.dumps(level)))
        counted[level] = handle_request({"operation": "read"} | request, project=recipe_project)["count"]
    expected = {level: len(support.EFFECTIVE_LEVELS_BY_RATING.get(level, [])) for level in ASIL_LEVELS}
    assert counted == expected


def test_the_count_recipe_comes_before_its_list() -> None:
    """Count first (rule 9): the list is the second request, asked only when the user asks which."""
    forms = [request["form"] for request in RECIPES["How many components are at a level"].requests]
    assert forms == ["count", "index"]


@pytest.mark.parametrize("title", ["Ports that nothing connects", "A component's unconnected ports"])
def test_the_unconnected_recipes_count_first_then_page_at_twenty(title: str) -> None:
    """The long-list rule: the number, then the first twenty — a large model's list is longer than one page."""
    requests = RECIPES[title].requests
    assert [request["form"] for request in requests] == ["count", "index"]
    assert "limit" not in requests[0]
    assert requests[1]["limit"] == 20
    assert {key: value for key, value in requests[1].items() if key not in ("form", "limit")} == {
        key: value for key, value in requests[0].items() if key != "form"
    }, "the page lists exactly what the count counts"


def test_the_one_component_recipe_is_kept_to_its_component_by_the_join(recipe_project: Any) -> None:
    """Without a second component with unconnected ports, a recipe that dropped its join would pass."""
    whole = support.EXPECTED["Ports that nothing connects"]["ports"]
    own = support.EXPECTED["A component's unconnected ports"]["ports"]
    assert {element for element, _ in whole} == {support.PLACEHOLDERS["<COMPONENT>"], "UnplacedSWC"}
    assert {element for element, _ in own} == {support.PLACEHOLDERS["<COMPONENT>"]}
    (counted, _) = (fill(request) for request in RECIPES["A component's unconnected ports"].requests)
    unjoined = {key: value for key, value in counted.items() if key != "join"}
    assert handle_request({"operation": "read"} | unjoined, project=recipe_project)["count"] == len(whole)


def test_the_recipe_model_has_a_same_named_element_on_another_interface(recipe_project: Any) -> None:
    """Without one, a readers/writers recipe that dropped its interface filter would pass."""
    for members in ("runnables.reads", "runnables.writes"):
        response = handle_request(
            {
                "operation": "read",
                "address": {"layer": "swc"},
                "form": "index",
                "members": members,
                "where": [{"field": "element", "equals": "value"}],
            },
            project=recipe_project,
        )
        assert "UnplacedSWC" in {row["element"] for row in response["value"]["identities"]}
    readers_and_writers = support.EXPECTED["Who reads or writes a data element"]
    assert all(access[0] != "UnplacedSWC" for accesses in readers_and_writers.values() for access in accesses)


def test_the_text_recipe_needs_every_word(recipe_project: Any) -> None:
    """Without this, a search that matched any one word would pass: ``REQ_EXAMPLE_001`` holds ``example`` alone."""
    (topic,) = fill(RECIPES["Requirements about a topic"].requests[0]).values()
    first, _ = topic.split()
    answer = handle_request({"operation": "find", "text": first}, project=recipe_project)
    assert "REQ_EXAMPLE_001" in [candidate["address"]["element"] for candidate in answer["candidates"]]
    assert "REQ_EXAMPLE_001" not in support.EXPECTED["Requirements about a topic"]


def test_the_stale_recipe_answers_the_link_not_the_requirement(recipe_project: Any) -> None:
    """``REQ_EXAMPLE_002`` has a current link beside its stale one.

    So a recipe that answered the requirement's every site would name ``IOther`` too.
    """
    sites = {row[1] for row in support.EXPECTED["Who realises a requirement"]}
    (stale,) = support.EXPECTED["Requirements nothing realises, and stale links"]["stale"]
    assert sites == {"LocalConsumerSWC", "IOther"}
    assert stale[2] == "LocalConsumerSWC"


def test_an_unknown_placeholder_is_refused() -> None:
    with pytest.raises(AssertionError, match="unknown placeholder"):
        fill({"address": {"layer": "swc"}, "where": [{"field": "name", "equals": "<NOT_A_NAME>"}]})


def test_the_section_extractor_stops_at_a_rule_and_at_a_heading() -> None:
    body = f"{SECTION_HEADING}\n\ntext\n"
    assert recipe_section(f"# P\n\n{body}---\n\n## Next\n") == body
    assert recipe_section(f"# P\n\n{body}## Next\n") == body


# The one loader: the profile's recipes and the How-to pages' recipe blocks.

_HOW_TO_PAGE = """# A page

## Steps { #steps }

<!-- recipe: Ports that nothing connects -->

Count first, then page.

```json
{"tool": "read", "arguments": {"address": {"layer": "swc"}, "form": "count", "members": "ports"}}
```

```json
{"tool": "read", "arguments": {"address": {"layer": "swc"}, "form": "index", "members": "ports", "limit": 20}}
```

<!-- recipe: What a change affects -->

```json
{"tool": "impact_of", "arguments": {"name": "<INTERFACE>", "form": "count", "direction": "up", "depth": 1}}
```

## After { #after }

```json
{"tool": "find", "arguments": {"names": ["X"]}}
```
"""


def test_a_how_to_recipe_block_is_a_recipe() -> None:
    first, second = parse_how_to_recipes(_HOW_TO_PAGE)
    assert first.title == "Ports that nothing connects"
    assert first.tool == "read"
    assert [request["form"] for request in first.requests] == ["count", "index"]
    assert "Count first, then page." in first.body
    assert second.title == "What a change affects"
    assert second.tool == "impact_of"
    assert len(second.requests) == 1, "a block ends at the next heading; the find after it is a step"


def test_a_recipe_block_naming_two_tools_is_refused() -> None:
    page = (
        "<!-- recipe: Mixed -->\n\n"
        '```json\n{"tool": "read", "arguments": {"address": {"layer": "swc"}, "form": "count"}}\n```\n\n'
        '```json\n{"tool": "find", "arguments": {"names": ["X"]}}\n```\n'
    )
    with pytest.raises(AssertionError, match="names one tool"):
        parse_how_to_recipes(page)


def test_a_page_without_recipe_blocks_has_no_recipe() -> None:
    assert parse_how_to_recipes('# A page\n\n```json\n{"tool": "find", "arguments": {}}\n```\n') == []


def test_the_one_loader_reads_the_profile_and_the_how_to_pages() -> None:
    """The profile's recipes first, in their order, then every How-to recipe block."""
    recipes = all_recipes()
    titles = [recipe.title for recipe in recipes]
    assert tuple(titles) == RECIPE_TITLES
    assert recipes[: len(PROFILE_RECIPE_TITLES)] == list(PROFILE_RECIPES.values())
    assert recipes[len(PROFILE_RECIPE_TITLES) :] == how_to_recipes()
    assert len(set(titles)) == len(titles), "every title once"
    assert all(isinstance(recipe, Recipe) and recipe.requests for recipe in recipes)
    assert HOW_TO_DIR.is_dir()


def _recipe(title: str) -> Recipe:
    return Recipe(title=title, tool="read", requests=({"address": {"layer": "swc"}, "form": "count"},))


def test_a_title_in_both_places_is_refused() -> None:
    profile = [_recipe("Ports that nothing connects"), _recipe("Tasks that run on a core")]
    how_to = [_recipe("Tasks that run on a core")]
    with pytest.raises(AssertionError, match=r"one source; .*\['Tasks that run on a core'\]"):
        one_source_per_title(profile, how_to)


def test_a_title_twice_in_one_source_is_refused() -> None:
    with pytest.raises(AssertionError, match=r"more than once in the How-to pages"):
        one_source_per_title([], [_recipe("A"), _recipe("A")])
    with pytest.raises(AssertionError, match=r"more than once in the profile"):
        one_source_per_title([_recipe("A"), _recipe("A")], [])


def test_distinct_titles_from_both_sources_are_returned_in_order() -> None:
    profile = [_recipe("A"), _recipe("B")]
    how_to = [_recipe("C")]
    assert one_source_per_title(profile, how_to) == [*profile, *how_to]


# The How-to recipe blocks: ``RECIPES`` holds them beside the profile's, so every gate above runs them too.


def test_the_how_to_pages_hold_exactly_their_recipes_once_each() -> None:
    titles = [recipe.title for recipe in how_to_recipes()]
    assert tuple(titles) == HOW_TO_RECIPE_TITLES
    assert len(set(titles)) == len(titles)

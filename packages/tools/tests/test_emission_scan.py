"""The emission scan's own planted cases: what it reads, and what it refuses to read."""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap

import pytest
from emission_scan import EmissionScan, Scopes

#: The imports the names the scan reads specially resolve to, put before each planted module.
_IMPORTS = "\n".join(
    [
        "from swcraft_tools.diagnostics import Subject",
        "from swcraft_tools.model_registry import current_registry",
        "from typing import NamedTuple",
        "",
        "",
    ]
)


def _scan(source: str, namespace: dict[str, object] | None = None, **options: frozenset[str]) -> EmissionScan:
    """The scan of the planted module *source*, dedented, after the imports."""
    return EmissionScan(_IMPORTS + textwrap.dedent(source), namespace or {}, **options)


def test_the_scan_refuses_a_subject_expression_it_cannot_read() -> None:
    """An unresolvable subjects argument is `None`, which the gate reports."""
    scan = _scan(
        """
        def check() -> None:
            records.append(Issue(rule="x", subjects=externally_supplied.subjects_for(thing)))
        """
    )
    (call,) = scan.sites()
    assert scan.layers(EmissionScan.keyword(call, "subjects")) is None


@pytest.mark.parametrize(
    ("case", "subjects"),
    [
        ("an empty literal", "()"),
        ("a list bound empty and never appended to in view", "tuple(collected)"),
    ],
    ids=["empty-literal", "empty-list"],
)
def test_the_scan_reads_an_empty_subject_container_as_no_layers(case: str, subjects: str) -> None:
    """The two shapes that resolve to `frozenset()` rather than to `None`.

    The empty set is the scan's intended answer: its `None` has to keep meaning
    *unreadable* for `_returns` and the tuple branch to propagate it, and
    `emission_layers` collapses the empty answer itself.
    """
    scan = _scan(
        f"""
        def check() -> None:
            collected = []
            records.append(Issue(rule="x", subjects={subjects}))
        """
    )
    call = next(site for site in scan.sites() if EmissionScan.keyword(site, "subjects") is not None)
    assert scan.layers(EmissionScan.keyword(call, "subjects")) == frozenset(), case


#: A reporter class of callbacks, built from a nested closure that emits, and one
#: phase calling a field of it on a parameter (`check_identifier_conformance`'s shape).
_REPORTER_SOURCE = """
class _Reporters(NamedTuple):
    declared: _DeclaredReport

class _Other(NamedTuple):
    declared: _DeclaredReport

def _reporters(records) -> _Reporters:
    def _report(name, at):
        records.append(Issue(rule="x", subjects=(at,)))
    return _Reporters(declared=_report)

def _phase(name, report{annotation}):
    report.{field}(name, Subject("swc", name))
"""


@pytest.mark.parametrize(
    ("annotation", "field", "expected"),
    [
        (": _Reporters", "declared", frozenset({"swc"})),
        ("", "declared", None),
        (": _Other", "declared", None),
        (": _Reporters", "generated", None),
    ],
    ids=["reporter-parameter", "unannotated-parameter", "class-never-bound-to-the-closure", "undeclared-field"],
)
def test_the_scan_follows_a_call_through_a_reporter_class_parameter(
    annotation: str, field: str, expected: frozenset[str] | None
) -> None:
    """`report.declared(…)` on a parameter annotated with the reporter class reaches the closure; nothing else does."""
    scan = _scan(_REPORTER_SOURCE.format(annotation=annotation, field=field))
    (call,) = scan.sites()
    assert scan.layers(EmissionScan.keyword(call, "subjects")) == expected


#: A finding whose subject layer is the loop variable over the registry's
#: declared layer names, the registry bound in the body in either of the two
#: spellings the validator uses.
_REGISTRY_LOOP_SOURCE = """
def records(stored, *, registry=None):
    registry = {binding}
    out = []
    for layer in registry.layer_names:
        for name in stored.get(layer, ()):
            out.append(Issue(rule="x", subjects=(Subject(layer, name),)))
    return out
"""


@pytest.mark.parametrize(
    "binding",
    ["registry or current_registry()", "current_registry()"],
    ids=["resolved-or-ambient", "ambient"],
)
def test_the_scan_reads_a_loop_over_the_registrys_declared_layers_as_those_layers(binding: str) -> None:
    """`for layer in registry.layer_names: … Subject(layer, …)` answers the contract's declared layers."""
    scan = _scan(_REGISTRY_LOOP_SOURCE.format(binding=binding), declared_layers=frozenset({"swc", "tasks"}))
    (call,) = scan.sites()
    assert scan.layers(EmissionScan.keyword(call, "subjects")) == frozenset({"swc", "tasks"})


_UNBOUND_LOOP_SOURCES: dict[str, str] = {
    # The loop iterates something the scan cannot tie to the registry.
    "a loop over a parameter": """
        def records(layers, names):
            out = []
            for layer in layers:
                out.append(Issue(rule="x", subjects=(Subject(layer, names[layer]),)))
            return out
        """,
    # The registry's layer FACTS are not its names: `Subject(layer, …)` would
    # carry a `LayerFacts`, and the scan must not read that as a layer.
    "a loop over the registry's layer facts": """
        def records(names, *, registry=None):
            registry = registry or current_registry()
            out = []
            for layer in registry.layers:
                out.append(Issue(rule="x", subjects=(Subject(layer, names[layer]),)))
            return out
        """,
    # `.layer_names` on something that is not the resolved registry.
    "a loop over another object's layer names": """
        def records(project, names):
            catalogue = project.catalogue()
            out = []
            for layer in catalogue.layer_names:
                out.append(Issue(rule="x", subjects=(Subject(layer, names[layer]),)))
            return out
        """,
    # A tuple target is not the loop variable the shape names.
    "a tuple target over a mapping": """
        def records(stored):
            out = []
            for layer, name in stored.items():
                out.append(Issue(rule="x", subjects=(Subject(layer, name),)))
            return out
        """,
    # A registry accessor this module defines is not the one the shape names.
    "a loop over a registry from an accessor defined here": """
        def current_registry():
            return elsewhere

        def records(names, *, registry=None):
            registry = registry or current_registry()
            out = []
            for layer in registry.layer_names:
                out.append(Issue(rule="x", subjects=(Subject(layer, names[layer]),)))
            return out
        """,
    # Bound by the loop, used after it: Python keeps the last value, and the
    # scan does not follow a variable out of its loop.
    "the loop variable used after its loop": """
        def records(names, *, registry=None):
            registry = registry or current_registry()
            for layer in registry.layer_names:
                pass
            return [Issue(rule="x", subjects=(Subject(layer, names[0]),))]
        """,
}


@pytest.mark.parametrize("case", sorted(_UNBOUND_LOOP_SOURCES), ids=sorted(_UNBOUND_LOOP_SOURCES))
def test_a_loop_variable_not_bound_to_the_declared_layers_still_fails_the_scan(case: str) -> None:
    """Anything but the declared-layer loop stays unreadable, so the gate fails rather than invents layers."""
    scan = _scan(_UNBOUND_LOOP_SOURCES[case], declared_layers=frozenset({"swc", "tasks"}))
    (call,) = scan.sites()
    assert scan.layers(EmissionScan.keyword(call, "subjects")) is None, case


def test_a_scan_that_was_told_no_declared_layers_reads_the_registry_loop_as_unreadable() -> None:
    """The layer set is the contract's, handed in; a scan built without one invents none."""
    scan = _scan(_REGISTRY_LOOP_SOURCE.format(binding="current_registry()"))
    (call,) = scan.sites()
    assert scan.layers(EmissionScan.keyword(call, "subjects")) is None


def _site_layers(source: str, rule: str = "x") -> frozenset[str] | None:
    """The layers the one emission site of *rule* in *source* reports at."""
    scan = _scan(source)
    (call,) = [site for site in scan.sites() if scan.keyword_strings(site) == {rule}]
    return scan.layers(EmissionScan.keyword(call, "subjects"))


def test_two_nested_helpers_of_one_name_each_resolve_to_their_own_body() -> None:
    source = """
        def first(name):
            def helper():
                return (Subject("swc", name),)
            records.append(Issue(rule="a", subjects=helper()))

        def second(name):
            def helper():
                return (Subject("tasks", name),)
            records.append(Issue(rule="b", subjects=helper()))
        """
    assert _site_layers(source, "a") == frozenset({"swc"})
    assert _site_layers(source, "b") == frozenset({"tasks"})


def test_callback_protocols_beside_the_sites_change_no_sites_layers() -> None:
    sites = """
        def subjects(name):
            return (Subject("swc", name),)

        def check(name):
            records.append(Issue(rule="x", subjects=subjects(name)))
        """
    protocols = """
        class _DeclaredReport(Protocol):
            def __call__(self, name: str, at: Subject) -> None: ...

        class _GeneratedReport(Protocol):
            def __call__(self, name: str, at: Subject) -> None: ...

        class _GeneratedPairReport(Protocol):
            def __call__(self, first: str, second: str, at: Subject) -> None: ...
        """
    combined = textwrap.dedent(protocols) + textwrap.dedent(sites)
    assert _site_layers(combined) == _site_layers(sites) == frozenset({"swc"})


def test_a_method_named_like_a_module_function_is_not_taken_for_it() -> None:
    source = """
        class Holder:
            def subjects(self, name):
                return (Subject("tasks", name),)

        def subjects(name):
            return (Subject("swc", name),)

        def check(name):
            records.append(Issue(rule="x", subjects=subjects(name)))
        """
    assert _site_layers(source) == frozenset({"swc"})


def test_an_inner_assignment_shadowing_an_outer_def_resolves_to_nothing() -> None:
    source = """
        def helper(name):
            return (Subject("swc", name),)

        def check(name, other):
            helper = other
            records.append(Issue(rule="x", subjects=helper(name)))
        """
    assert _site_layers(source) is None


def test_a_callees_return_is_read_in_the_callees_scope() -> None:
    source = """
        def subjects(name):
            def helper():
                return (Subject("tasks", name),)
            return helper()

        def check(name):
            def helper():
                return (Subject("swc", name),)
            records.append(Issue(rule="x", subjects=subjects(name)))
        """
    assert _site_layers(source) == frozenset({"tasks"})


def test_a_return_inside_a_nested_function_of_the_callee_is_not_collected() -> None:
    source = """
        def subjects(name):
            def unused():
                return (Subject("tasks", name),)
            return (Subject("swc", name),)

        def check(name):
            records.append(Issue(rule="x", subjects=subjects(name)))
        """
    assert _site_layers(source) == frozenset({"swc"})


def test_a_class_body_call_resolves_in_the_class_and_a_method_body_skips_it() -> None:
    source = """
        def helper():
            return (Subject("swc", "a"),)

        class Holder:
            def helper():
                return (Subject("tasks", "b"),)

            ISSUE = Issue(rule="in-class", subjects=helper())

            def check(self):
                records.append(Issue(rule="in-method", subjects=helper()))
        """
    assert _site_layers(source, "in-class") == frozenset({"tasks"})
    assert _site_layers(source, "in-method") == frozenset({"swc"})


@pytest.mark.parametrize(
    ("decorator", "expected"),
    [
        ("import functools\n\n\n@functools.cache", frozenset({"swc"})),
        ("from functools import lru_cache\n\n\n@lru_cache(maxsize=None)", frozenset({"swc"})),
        ("@replaced_by_another", None),
    ],
    ids=["cache", "lru-cache", "outside-the-allow-list"],
)
def test_a_decorated_def_resolves_only_under_a_decorator_that_keeps_its_return(
    decorator: str, expected: frozenset[str] | None
) -> None:
    source = decorator + textwrap.dedent(
        """
        def subjects(name):
            return (Subject("swc", name),)

        def check(name):
            records.append(Issue(rule="x", subjects=subjects(name)))
        """
    )
    assert _site_layers(source) == expected


def test_two_defs_of_one_name_in_one_function_scope_are_a_collision() -> None:
    source = """
        def check(flag, name):
            if flag:
                def helper():
                    return (Subject("swc", name),)
            else:
                def helper():
                    return (Subject("tasks", name),)
            records.append(Issue(rule="x", subjects=helper()))
        """
    scan = _scan(source)
    assert scan.scopes.collisions() == ["check: helper"]
    assert _site_layers(source) is None


def test_an_overload_group_in_a_class_body_is_not_a_collision() -> None:
    source = """
        class Holder:
            @overload
            def get(self, key: int) -> int: ...
            @overload
            def get(self, key: str) -> str: ...
            def get(self, key):
                return key
        """
    scan = _scan(source)
    assert scan.scopes.collisions() == []
    assert scan.scopes.unpaired() == []


def test_a_name_declared_global_in_the_calling_scope_resolves_to_nothing() -> None:
    source = """
        def helper(name):
            return (Subject("swc", name),)

        def check(name):
            global helper
            records.append(Issue(rule="x", subjects=helper(name)))
        """
    assert _site_layers(source) is None


def test_a_name_declared_nonlocal_in_the_calling_scope_resolves_to_nothing() -> None:
    source = """
        def check(name):
            def helper():
                return (Subject("swc", name),)

            def inner():
                nonlocal helper
                records.append(Issue(rule="x", subjects=helper()))
        """
    assert _site_layers(source) is None


@pytest.mark.parametrize(
    "decorator",
    ["@staticmethod", "from functools import cache\n\n\n@cache", "from functools import lru_cache\n\n\n@lru_cache"],
    ids=["staticmethod", "bare-cache", "bare-lru-cache"],
)
def test_a_def_under_a_decorator_on_the_allow_list_resolves(decorator: str) -> None:
    source = decorator + textwrap.dedent(
        """
        def subjects(name):
            return (Subject("swc", name),)

        def check(name):
            records.append(Issue(rule="x", subjects=subjects(name)))
        """
    )
    assert _site_layers(source) == frozenset({"swc"})


@pytest.mark.parametrize(
    "shadow",
    [
        "def check(name, helper):",
        "def check(name):\n    from elsewhere import helper",
        "def check(name):\n    for helper in handlers:\n        pass",
        "def check(name):\n    [helper for helper in handlers]\n    helper = handlers[0]",
    ],
    ids=["parameter", "import", "loop-target", "assignment-beside-a-comprehension-variable"],
)
def test_a_local_binding_of_any_kind_shadows_the_module_def(shadow: str) -> None:
    """A parameter, an import or a loop target binds the name locally, so the module's `def` is not the callee."""
    source = (
        'def helper(name):\n    return (Subject("swc", name),)\n\n\n'
        + shadow
        + '\n    records.append(Issue(rule="x", subjects=helper(name)))\n'
    )
    assert _site_layers(source) is None


def test_a_comprehension_variable_does_not_shadow_the_name_outside_it() -> None:
    source = """
        def helper(name):
            return (Subject("swc", name),)

        def check(name):
            [helper for helper in handlers]
            records.append(Issue(rule="x", subjects=helper(name)))
        """
    assert _site_layers(source) == frozenset({"swc"})


def test_an_argument_passed_on_through_a_middle_function_is_the_outer_callers() -> None:
    source = """
        def subjects(layer_subject):
            return (layer_subject,)

        def middle(at):
            return subjects(at)

        def check(name):
            records.append(Issue(rule="x", subjects=middle(Subject("tasks", name))))
        """
    assert _site_layers(source) == frozenset({"tasks"})


def test_a_decorator_default_and_first_iterable_resolve_in_the_enclosing_scope() -> None:
    """Each of the three is evaluated outside the scope it belongs to, so a name the scope binds does not reach it."""
    source = """
        def helper():
            return (Subject("swc", "a"),)

        def check():
            @register(helper())
            def inner(at=helper()):
                def helper():
                    return (Subject("tasks", "b"),)
                return at
            [helper for helper in helper()]
        """
    scan = _scan(source)
    calls = [
        node
        for node in ast.walk(scan.tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "helper"
    ]
    assert len(calls) == 3
    module_def = next(node for node in scan.tree.body if isinstance(node, ast.FunctionDef))
    assert all(scan.scopes.resolve("helper", call) is module_def for call in calls)


def test_two_comprehensions_of_one_line_pair_with_their_own_tables() -> None:
    """A comprehension inside another's first iterable gets its table first, as `symtable` creates it."""
    source = (
        "def helper():\n    pass\n\n\n"
        "value = [helper() for a in items] + [helper for helper in [helper() for b in items]]\n"
    )
    scan = EmissionScan(source, {})
    calls = [
        node
        for node in ast.walk(scan.tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "helper"
    ]
    assert len(calls) == 2
    assert all(scan.scopes.resolve("helper", call) is scan.tree.body[0] for call in calls)
    assert scan.scopes.unpaired() == []


def test_a_scope_with_no_symtable_table_is_reported_unpaired() -> None:
    """A tree and a source that disagree leave the `def` without a table, which the gate refuses."""
    scopes = Scopes(ast.parse("def helper():\n    pass\n"), "\ndef helper():\n    pass\n")
    assert scopes.unpaired() == [1]


#: A helper that fills a list with `(owner, subject)` pairs, and a caller iterating it with a tuple target.
_TUPLE_LOOP_SOURCE = """
def element_texts(description):
    texts: list = []
    for unit in description.units:
        at = Subject("units", unit.name)
        texts.append((unit.name, at))
    texts.extend((entry.name, Subject("compu_methods", entry.name)) for entry in description.entries)
    return tuple(texts)

def _report(records, owner, at):
    records.append(Issue(rule="x", subjects=(at,)))

def check(description):
    records = []
    {before}
    for owner, at in {iterable}:
        _report(records, owner, at)
    return records
"""


@pytest.mark.parametrize(
    ("before", "iterable", "expected"),
    [
        ("", "element_texts(description)", frozenset({"units", "compu_methods"})),
        ('at = Subject("swc", "s")', "element_texts(description)", frozenset({"units", "compu_methods"})),
        ('at = Subject("swc", "s")', "description.pairs", None),
        ("", "elsewhere(description)", None),
    ],
    ids=["called-helper", "earlier-assignment-never-borrowed", "attribute-iterable", "unresolved-callee"],
)
def test_a_tuple_loop_target_reads_its_position_of_what_the_iterable_yields(
    before: str, iterable: str, expected: frozenset[str] | None
) -> None:
    """`for owner, at in f(…)` binds `at` to the second item of each element `f` returns; else it is unreadable."""
    assert _site_layers(_TUPLE_LOOP_SOURCE.format(before=before, iterable=iterable)) == expected


@pytest.mark.parametrize(
    ("fill", "expected"),
    [
        ('subjects += [Subject("swc", name)]', frozenset({"tasks", "swc"})),
        ('subjects.append(Subject("swc", name))', frozenset({"tasks", "swc"})),
        ("extend_elsewhere(subjects)", None),
        ("subjects = other", None),
    ],
    ids=["augmented", "appended", "passed-to-a-call", "rebound-to-the-unreadable"],
)
def test_a_list_built_in_steps_is_read_whole_or_not_at_all(fill: str, expected: frozenset[str] | None) -> None:
    """A list's every addition counts; a use that could add unseen, or a later rebinding, makes it unreadable."""
    source = f"""
        def check(names, other):
            for name in names:
                subjects = [Subject("tasks", name)]
                {fill}
                records.append(Issue(rule="x", subjects=tuple(subjects)))
        """
    assert _site_layers(source) == expected


def test_a_name_bound_in_both_arms_of_a_branch_reads_both() -> None:
    source = """
        def check(flag, name):
            if flag:
                at = Subject("swc", name)
            else:
                at = Subject("tasks", name)
            records.append(Issue(rule="x", subjects=(at,)))
        """
    assert _site_layers(source) == frozenset({"swc", "tasks"})


def test_a_binding_below_the_use_reaches_it_through_the_loop_unless_a_rebinding_above_kills_it() -> None:
    carried = """
        def check(names):
            at = Subject("swc", "s")
            for name in names:
                records.append(Issue(rule="x", subjects=(at,)))
                at = Subject("tasks", name)
        """
    killed = """
        def check(names):
            for name in names:
                at = Subject("swc", name)
                records.append(Issue(rule="x", subjects=(at,)))
                at = Subject("tasks", name)
        """
    assert _site_layers(carried) == frozenset({"swc", "tasks"})
    assert _site_layers(killed) == frozenset({"swc"})


def test_a_binding_the_scan_does_not_read_is_never_passed_over_for_an_earlier_one() -> None:
    source = """
        def check(name, opened):
            at = Subject("swc", name)
            with opened as at:
                records.append(Issue(rule="x", subjects=(at,)))
        """
    assert _site_layers(source) is None


@pytest.mark.parametrize(
    ("module", "rule", "layers"),
    [
        ("representable_text", "text.xml-unrepresentable-character", {"units", "compu_methods", "data_constraints"}),
        ("hardware", "task.runnable-cost-exceeds-task", {"tasks", "swc"}),
    ],
    ids=["tuple-loop-target", "augmented-list"],
)
def test_the_shipped_sites_read_through_these_shapes_name_their_true_layers(
    module: str, rule: str, layers: set[str]
) -> None:
    imported = importlib.import_module(f"swcraft_tools.validator.{module}")
    scan = EmissionScan(inspect.getsource(imported), vars(imported))
    observed = [
        scan.layers(EmissionScan.keyword(site, "subjects"))
        for site in scan.sites()
        if rule in (scan.keyword_strings(site) or set())
    ]
    assert observed and all(site is not None and layers <= site for site in observed), observed


def test_a_list_copied_from_elsewhere_and_then_appended_to_is_unreadable() -> None:
    source = """
        def check(name, base):
            subjects = list(base)
            subjects.append(Subject("swc", name))
            records.append(Issue(rule="x", subjects=tuple(subjects)))
        """
    assert _site_layers(source) is None


@pytest.mark.parametrize(
    "source",
    [
        """
        def check(name):
            first, *rest, at = (Subject("tasks", name), Subject("tasks", name), Subject("swc", name))
            records.append(Issue(rule="x", subjects=(at,)))
        """,
        """
        def rows(name):
            return [(Subject("tasks", name), Subject("tasks", name), Subject("swc", name))]

        def check(name):
            for first, *rest, at in rows(name):
                records.append(Issue(rule="x", subjects=(at,)))
        """,
    ],
    ids=["assignment", "loop-target"],
)
def test_a_name_beside_a_starred_target_is_unreadable(source: str) -> None:
    """Its position counts from the end, which the scan does not follow."""
    assert _site_layers(source) is None


def test_an_assignment_through_nonlocal_makes_the_name_unreadable() -> None:
    source = """
        def check(name):
            at = Subject("tasks", name)
            def change():
                nonlocal at
                at = Subject("swc", name)
            change()
            records.append(Issue(rule="x", subjects=(at,)))
        """
    assert _site_layers(source) is None


def test_a_nonlocal_name_only_read_keeps_the_binding_readable() -> None:
    source = """
        def check(name):
            at = Subject("tasks", name)
            def show():
                nonlocal at
                return at
            records.append(Issue(rule="x", subjects=(at,)))
        """
    assert _site_layers(source) == frozenset({"tasks"})


#: A list reached through each kind of binding, then used once by `{use}`; `fill` adds to a list it is given.
_BOUND_LIST_SOURCES = {
    "call": """
        def make(name):
            return [Subject("tasks", name)]

        def fill(xs, name):
            xs.append(Subject("swc", name))

        def check(name, other):
            subjects = make(name)
            {use}
            records.append(Issue(rule="x", subjects=tuple(subjects)))
        """,
    "parameter": """
        def fill(xs, name):
            xs.append(Subject("swc", name))

        def _emit(subjects, name, other):
            {use}
            records.append(Issue(rule="x", subjects=tuple(subjects)))

        def check(name, other):
            _emit([Subject("tasks", name)], name, other)
        """,
    "loop-element": """
        def fill(xs, name):
            xs.append(Subject("swc", name))

        def check(name, other):
            for subjects in [[Subject("tasks", name)]]:
                {use}
                records.append(Issue(rule="x", subjects=tuple(subjects)))
        """,
    "unpacked-item": """
        def fill(xs, name):
            xs.append(Subject("swc", name))

        def check(name, other):
            subjects, count = [Subject("tasks", name)], 1
            {use}
            records.append(Issue(rule="x", subjects=tuple(subjects)))
        """,
}

#: Each use, and what the list then holds: the true layers where the scan follows the use, else `None`.
_LIST_USES = {
    "append": ('subjects.append(Subject("swc", name))', frozenset({"tasks", "swc"})),
    "passed-to-a-filling-function": ("fill(subjects, name)", frozenset({"tasks", "swc"})),
    "insert": ('subjects.insert(0, Subject("swc", name))', None),
    "item-assignment": ('subjects[0] = Subject("swc", name)', None),
    "item-deletion": ("del subjects[0]", None),
    "alias": ('alias = subjects\n    alias.append(Subject("swc", name))', None),
    "passed-to-an-unknown-call": ("other.extend_into(subjects)", None),
    "read-only": ("assert len(subjects) > 0", frozenset({"tasks"})),
}


@pytest.mark.parametrize("binding", sorted(_BOUND_LIST_SOURCES))
@pytest.mark.parametrize("use", sorted(_LIST_USES))
def test_a_list_is_read_with_what_its_uses_add_whatever_binds_it(binding: str, use: str) -> None:
    """Every use of the list in place is followed or refused, whether a call, a parameter or a target binds it."""
    statement, expected = _LIST_USES[use]
    indent = " " * 16 if binding == "loop-element" else " " * 12
    source = _BOUND_LIST_SOURCES[binding].replace("{use}", statement.replace("\n    ", "\n" + indent))
    assert _site_layers(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        """
        def texts(base, name):
            base.append((name, Subject("swc", name)))
            return base

        def check(name):
            for owner, at in texts([(name, Subject("units", name))], name):
                records.append(Issue(rule="x", subjects=(at,)))
        """,
        """
        def seed(name):
            return [(name, Subject("units", name))]

        def texts(name):
            out = seed(name)
            out.insert(0, (name, Subject("swc", name)))
            return out

        def check(name):
            for owner, at in texts(name):
                records.append(Issue(rule="x", subjects=(at,)))
        """,
        """
        def check(name, other):
            subjects = {Subject("tasks", name)}
            subjects |= other
            records.append(Issue(rule="x", subjects=tuple(subjects)))
        """,
    ],
    ids=["callee-fills-its-parameter", "callee-inserts-into-a-called-list", "set-union-in-place"],
)
def test_a_collection_changed_in_place_by_the_function_it_is_read_through_is_unreadable(source: str) -> None:
    assert _site_layers(source) is None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            """
            def check(name, flag):
                at = Subject("tasks", name)
                flag and (at := Subject("swc", name))
                records.append(Issue(rule="x", subjects=(at,)))
            """,
            frozenset({"tasks", "swc"}),
        ),
        (
            """
            def check(name):
                at = Subject("tasks", name)
                pending = (at for _ in [0])
                at = Subject("swc", name)
                records.append(Issue(rule="x", subjects=tuple(pending)))
            """,
            None,
        ),
        (
            """
            def check(name):
                at = Subject("tasks", name)
                records.append(Issue(rule="x", subjects=tuple(at for _ in [0])))
                at = Subject("swc", name)
            """,
            frozenset({"tasks"}),
        ),
        (
            """
            def check(name):
                subjects = [Subject("tasks", name)]
                def more():
                    nonlocal subjects
                    subjects.append(Subject("swc", name))
                more()
                records.append(Issue(rule="x", subjects=tuple(subjects)))
            """,
            frozenset({"tasks", "swc"}),
        ),
        (
            """
            def check(name):
                def helper():
                    return (Subject("tasks", name),)
                def swap():
                    nonlocal helper
                    def helper():
                        return (Subject("swc", name),)
                swap()
                records.append(Issue(rule="x", subjects=helper()))
            """,
            None,
        ),
        (
            """
            def tuple(xs):
                return (Subject("swc", "n"),)

            def check(name):
                subjects = [Subject("tasks", name)]
                records.append(Issue(rule="x", subjects=tuple(subjects)))
            """,
            frozenset({"swc"}),
        ),
        (
            """
            def emit(unused, at):
                records.append(Issue(rule="x", subjects=(at,)))

            def check(name):
                emit(*[], Subject("tasks", name), Subject("swc", name))
            """,
            None,
        ),
    ],
    ids=[
        "walrus-in-a-condition-ends-no-earlier-binding",
        "generator-consumed-after-a-rebinding",
        "generator-consumed-at-once",
        "list-filled-through-nonlocal",
        "function-rebound-through-nonlocal",
        "copying-builtin-shadowed-by-a-module-def",
        "argument-after-a-starred-one",
    ],
)
def test_a_name_python_binds_late_or_elsewhere_reads_true_or_unreadable(
    source: str, expected: frozenset[str] | None
) -> None:
    """Each shape answers the layers Python's binding gives, or `None`, never an earlier or shadowed binding's."""
    assert _site_layers(source) == expected


#: Each name, method or construct the scan reads specially, bound the way the scan assumes or not, and its true answer.
_SPECIAL_READINGS: dict[str, tuple[str, dict[str, object], frozenset[str] | None]] = {
    "class-body-call-before-the-class-def": (
        """
        def helper():
            return (Subject("swc", "a"),)

        class Holder:
            ISSUE = Issue(rule="x", subjects=helper())

            def helper():
                return (Subject("tasks", "b"),)
        """,
        {},
        frozenset({"swc"}),
    ),
    "bound-method-alias-of-the-list": (
        """
        def check(name):
            subjects = [Subject("tasks", name)]
            add = subjects.append
            add(Subject("swc", name))
            records.append(Issue(rule="x", subjects=tuple(subjects)))
        """,
        {},
        None,
    ),
    "bound-method-passed-on": (
        """
        def check(name, more):
            subjects = [Subject("tasks", name)]
            run_each(subjects.append, more)
            records.append(Issue(rule="x", subjects=tuple(subjects)))
        """,
        {},
        None,
    ),
    "local-name-shadowing-a-module-constant": (
        """
        LAYER = "tasks"

        def check(name):
            LAYER = "swc"
            records.append(Issue(rule="x", subjects=(Subject(LAYER, name),)))
        """,
        {"LAYER": "tasks"},
        None,
    ),
    "module-constant": (
        """
        LAYER = "tasks"

        def check(name):
            records.append(Issue(rule="x", subjects=(Subject(LAYER, name),)))
        """,
        {"LAYER": "tasks"},
        frozenset({"tasks"}),
    ),
    "generator-copied": (
        """
        def _helper(name):
            yield Subject("swc", name)
            return (Subject("tasks", name),)

        def check(name):
            records.append(Issue(rule="x", subjects=tuple(_helper(name))))
        """,
        {},
        frozenset({"swc"}),
    ),
    "generator-unpacked-beside-a-subject": (
        """
        def _helper(name):
            yield Subject("swc", name)

        def check(name):
            records.append(Issue(rule="x", subjects=(Subject("tasks", name), *_helper(name))))
        """,
        {},
        frozenset({"tasks", "swc"}),
    ),
    "generator-iterated": (
        """
        def _helper(name):
            yield Subject("swc", name)
            return (Subject("tasks", name),)

        def check(name):
            for at in _helper(name):
                records.append(Issue(rule="x", subjects=(at,)))
        """,
        {},
        frozenset({"swc"}),
    ),
    "coroutine-function": (
        """
        async def _helper(name):
            return (Subject("tasks", name),)

        def check(name):
            records.append(Issue(rule="x", subjects=_helper(name)))
        """,
        {},
        None,
    ),
    "decorator-named-cache-defined-here": (
        """
        def cache(function):
            def wrapper(name):
                return (Subject("swc", name),)
            return wrapper

        @cache
        def subjects(name):
            return (Subject("tasks", name),)

        def check(name):
            records.append(Issue(rule="x", subjects=subjects(name)))
        """,
        {},
        None,
    ),
    "local-function-named-subject": (
        """
        from swcraft_tools.diagnostics import Subject as Real

        def check(name):
            def Subject(layer, key):
                return Real("swc", key)
            records.append(Issue(rule="x", subjects=(Subject("tasks", name),)))
        """,
        {},
        frozenset({"swc"}),
    ),
    "subjects-keyword-of-a-filling-function": (
        """
        def _stash(subjects):
            subjects.append(Subject("swc", "n"))

        def check(name):
            xs = [Subject("tasks", name)]
            _stash(subjects=xs)
            records.append(Issue(rule="x", subjects=tuple(xs)))
        """,
        {},
        frozenset({"tasks", "swc"}),
    ),
    "public-function-parameter": (
        """
        def emit(at):
            records.append(Issue(rule="x", subjects=(at,)))

        def check(name):
            emit(Subject("tasks", name))
        """,
        {},
        None,
    ),
    "nested-function-handed-out": (
        """
        def check(name):
            def _emit(at):
                records.append(Issue(rule="x", subjects=(at,)))
            _emit(Subject("tasks", name))
            return _emit
        """,
        {},
        None,
    ),
    "reporter-built-positionally": (
        """
        class _Reporter(NamedTuple):
            report: Callable

        def _emit(name, at):
            records.append(Issue(rule="x", subjects=(at,)))

        def _run(reporter: _Reporter, name):
            reporter.report(name, Subject("swc", name))

        def check(name):
            _emit(name, Subject("tasks", name))
            _run(_Reporter(_emit), name)
        """,
        {},
        None,
    ),
    "reporter-instance-handed-out": (
        """
        class _Reporter(NamedTuple):
            report: Callable

        def _emit(name, at):
            records.append(Issue(rule="x", subjects=(at,)))

        def _run(reporter: _Reporter, name):
            reporter.report(name, Subject("tasks", name))

        def check(name):
            reporter = _Reporter(report=_emit)
            _run(reporter, name)
            elsewhere(reporter)
        """,
        {},
        None,
    ),
}


@pytest.mark.parametrize("case", sorted(_SPECIAL_READINGS), ids=sorted(_SPECIAL_READINGS))
def test_a_name_or_construct_the_scan_reads_specially_counts_only_bound_as_it_assumes(case: str) -> None:
    """A special reading holds only where the name resolves to what the scan assumes; elsewhere true or `None`."""
    source, namespace, expected = _SPECIAL_READINGS[case]
    scan = _scan(source, namespace)
    (call,) = [site for site in scan.sites() if scan.keyword_strings(site) == {"x"}]
    assert scan.layers(EmissionScan.keyword(call, "subjects")) == expected


@pytest.mark.parametrize(
    ("layer", "expected"),
    [
        ('"swc" if flag else kind', None),
        ('kind or "swc"', None),
        ('"swc" if flag else "tasks"', frozenset({"swc", "tasks"})),
    ],
    ids=["conditional-unreadable-arm", "or-unreadable-operand", "conditional-two-literals"],
)
def test_a_conditional_layer_reads_every_arm_or_is_unreadable(layer: str, expected: frozenset[str] | None) -> None:
    source = f"""
        def check(name, flag):
            kind = "tasks"
            records.append(Issue(rule="x", subjects=(Subject({layer}, name),)))
        """
    assert _site_layers(source) == expected


def test_a_rule_keyword_read_in_part_attributes_the_site_to_nothing() -> None:
    scan = _scan(
        """
        def check(name, flag, other):
            records.append(Issue(rule="x" if flag else other, subjects=(Subject("swc", name),)))
            records.append(Issue(rule="x" if flag else "y", subjects=(Subject("swc", name),)))
        """
    )
    assert [scan.keyword_strings(site) for site in scan.sites()] == [None, {"x", "y"}]


@pytest.mark.parametrize(
    "change",
    ['TABLE["b"] = "tasks"', 'TABLE.update(b="tasks")', 'del TABLE["a"]', "_register(TABLE)"],
    ids=["item-assignment", "mutating-method", "item-deletion", "passed-to-a-function"],
)
def test_a_module_table_any_use_could_change_is_unreadable(change: str) -> None:
    source = f"""
        TABLE = {{"a": "swc"}}

        def check(name, key):
            {change}
            records.append(Issue(rule="x", subjects=(Subject(TABLE[key], name),)))
        """
    scan = _scan(source, {"TABLE": {"a": "swc"}})
    (call,) = scan.sites()
    assert scan.layers(EmissionScan.keyword(call, "subjects")) is None
    assert scan.table_strings(call) == set()


def test_a_module_table_only_read_answers_its_values() -> None:
    source = """
        TABLE = {"a": "swc"}

        def check(name, key):
            if key in TABLE and len(TABLE) and TABLE.get(key):
                for each in TABLE.items():
                    records.append(Issue(rule="x", subjects=(Subject(TABLE[key], name),)))
        """
    scan = _scan(source, {"TABLE": {"a": "swc"}})
    (call,) = scan.sites()
    assert scan.layers(EmissionScan.keyword(call, "subjects")) == frozenset({"swc"})


@pytest.mark.parametrize(
    "alias",
    [
        "xs, n = subjects, 0",
        "for xs in (subjects,):\n                pass",
        "for xs, n in zip([subjects], [0]):\n                pass",
        "xs = [each for each in (subjects,)][0]",
        "(xs,) = [subjects]",
        "for xs in tuple([subjects]):\n                pass",
    ],
    ids=["parallel-assignment", "loop-over-display", "zip", "comprehension-over-display", "unpacked-list", "copy"],
)
def test_a_display_unpacked_into_a_new_name_makes_the_collection_unreadable(alias: str) -> None:
    source = f"""
        def check(name):
            subjects = [Subject("tasks", name)]
            {alias}
            xs.append(Subject("swc", name))
            records.append(Issue(rule="x", subjects=tuple(subjects)))
        """
    assert _site_layers(source) is None


def test_a_display_only_copied_from_keeps_the_collection_readable() -> None:
    source = """
        def check(name):
            subjects = [Subject("tasks", name)]
            both = [*subjects, Subject("swc", name)]
            records.append(Issue(rule="x", subjects=tuple(subjects)))
        """
    assert _site_layers(source) == frozenset({"tasks"})


@pytest.mark.parametrize(
    "method",
    ["__init__", "__setitem__", "fill_from", "extend_all"],
    ids=["dunder-init", "dunder-setitem", "unknown-method", "unknown-filling-method"],
)
def test_a_method_off_the_closed_unchanging_list_makes_the_collection_unreadable(method: str) -> None:
    source = f"""
        def check(name):
            subjects = [Subject("tasks", name)]
            subjects.{method}([Subject("swc", name)])
            records.append(Issue(rule="x", subjects=tuple(subjects)))
        """
    assert _site_layers(source) is None


def test_a_method_taken_off_the_closed_unchanging_list_without_a_call_makes_the_collection_unreadable() -> None:
    source = """
        def check(name):
            subjects = [Subject("tasks", name)]
            fill = subjects.__init__
            fill([Subject("swc", name)])
            records.append(Issue(rule="x", subjects=tuple(subjects)))
        """
    assert _site_layers(source) is None


def test_the_reading_methods_and_the_subjects_own_members_keep_the_collection_readable() -> None:
    source = """
        def check(name):
            at = Subject("tasks", name)
            subjects = [at.at(), Subject("swc", name)]
            if subjects.count(name) and subjects.index(name) and at.layer and at.element:
                records.append(Issue(rule="x", subjects=tuple(subjects)))
        """
    assert _site_layers(source) == frozenset({"tasks", "swc"})


#: A reporter class whose construction `_reporters` binds to `_report`; `{header}` and `{extra}` shape the class.
_REPORTER_CONSTRUCTOR_SOURCE = """
{header}
    \"\"\"The callbacks.\"\"\"

    declared: Callable
{extra}

def _report(name, at):
    records.append(Issue(rule="x", subjects=(at,)))

def _reporters() -> _Reporter:
    return _Reporter(declared=_report)

def _phase(name, report: _Reporter):
    report.declared(name, Subject("tasks", name))
"""

_SWAPPING_INIT = """
    def __init__(self, declared):
        self.declared = lambda name, at: declared(name, Subject("swc", name))
"""


@pytest.mark.parametrize(
    ("header", "extra", "expected"),
    [
        ("class _Reporter(NamedTuple):", "", frozenset({"tasks"})),
        ("class _Reporter:", _SWAPPING_INIT, None),
        ("class _Reporter(NamedTuple):", _SWAPPING_INIT, None),
        ("@dataclass\nclass _Reporter:", "", None),
        ("class _Reporter(_Base):", "", None),
        ("class _Reporter(NamedTuple, metaclass=_Meta):", "", None),
        ("class _Reporter(NamedTuple):", "    other: Callable = _report", None),
    ],
    ids=[
        "named-tuple-of-fields",
        "own-constructor",
        "named-tuple-with-a-constructor",
        "dataclass",
        "unread-base",
        "class-keyword",
        "field-with-a-default",
    ],
)
def test_a_reporter_class_counts_only_when_its_constructor_stores_the_fields_as_passed(
    header: str, extra: str, expected: frozenset[str] | None
) -> None:
    scan = _scan(_REPORTER_CONSTRUCTOR_SOURCE.format(header=header, extra=extra))
    (call,) = scan.sites()
    assert scan.layers(EmissionScan.keyword(call, "subjects")) == expected

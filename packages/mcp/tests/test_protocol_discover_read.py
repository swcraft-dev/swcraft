"""The two unscoped protocol tools, ``discover`` and ``read``, driven over the wire.

Every exchange goes through ``tools/call`` (``protocol_client``): the argument binding, the body
block and the transport-level tool error are FastMCP's and the server's, not the function body's.
The tools are pass-throughs, so a response's content is held byte for byte to what
``handle_request`` answers for the same request.

No storage root leaves the surface: asserted here, at the boundary a client reads, so a leak
added by a wrapper in ``server.py`` fails even where the engine's own answer is clean.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from protocol_client import audit_records, call
from swcraft_mcp.server import HOST_DISCOVER_DEFAULT
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.validator import DERIVED

#: The keys kept off the wire: the manifest's resolved storage roots, and `model_hash`, which
#: the protocol replaces with the `baseline` token a client states back.
FORBIDDEN_KEYS = frozenset({"project_root", "model_root", "dist_root", "views_root", "model_hash"})


def _manifest() -> str:
    """The manifest the autouse ``_project_env`` fixture pointed the server at."""
    import os

    return os.environ["SWCRAFT_MANIFEST"]


def _engine(request: dict[str, Any], discover_default: str | None = None) -> dict[str, Any]:
    """The same request, answered by the engine directly.

    *discover_default* is ``handle_request``'s channel keyword: the server passes
    :data:`~swcraft_mcp.server.HOST_DISCOVER_DEFAULT` on every call, so a wire ``discover`` is
    compared under that value. A ``read`` ignores it; passing nothing keeps ``summary``.
    """
    return handle_request(request, project=_manifest(), discover_default=discover_default)  # type: ignore[arg-type]


def _walk_keys(node: Any) -> set[str]:
    """Every mapping key at every depth of *node*."""
    if isinstance(node, dict):
        keys = set(node)
        for value in node.values():
            keys |= _walk_keys(value)
        return keys
    if isinstance(node, list):
        return set().union(*(_walk_keys(item) for item in node)) if node else set()
    return set()


class TestDiscover:
    def test_project_wide_discovery_is_the_engines_answer(self) -> None:
        structured, _texts = call("discover")
        assert structured == _engine({"operation": "discover"}, HOST_DISCOVER_DEFAULT)
        assert structured["response"] == "discover"
        assert set(structured["project"]) == {"name", "arxml_version", "swcraft_version"}
        assert structured["baseline"]
        # The host's default is `baseline`: no layer descriptor. The project-wide `summary`,
        # asked by name, carries every declared layer.
        assert structured["layers"] == []
        summary, _texts = call("discover", form="summary")
        assert summary == _engine({"operation": "discover", "form": "summary"}, HOST_DISCOVER_DEFAULT)
        assert len(summary["layers"]) == 15

    def test_discovery_narrowed_to_one_layer_answers_with_that_layer_alone(self) -> None:
        structured, _texts = call("discover", layer="implementation_types")
        assert structured == _engine({"operation": "discover", "layer": "implementation_types"}, HOST_DISCOVER_DEFAULT)
        assert [layer["layer"] for layer in structured["layers"]] == ["implementation_types"]

    def test_a_declared_but_empty_layer_is_reported_present(self) -> None:
        # `_project_env` creates every layer directory and seeds no hardware element: a layer
        # that is there and holds nothing, as against one that is absent (a project defect).
        structured, _texts = call("discover", layer="hardware")
        assert structured["layers"][0]["present"] is True
        # The default `summary` omits `optional: false` (absent means `false`)
        # and never `present`, whichever way the probe answers.
        assert "optional" not in structured["layers"][0]

    def test_an_undeclared_layer_is_unknown_field(self) -> None:
        structured, _texts = call("discover", layer="nosuchlayer")
        assert structured == _engine({"operation": "discover", "layer": "nosuchlayer"})
        assert structured["response"] == "error"
        assert structured["error"] == "unknown-field"
        assert structured["addresses"] == [{"layer": "nosuchlayer"}]

    def test_the_reads_block_reaches_the_wire_with_the_servers_own_constants(self) -> None:
        # A client learns the forms and the budget from `discover`, never from the prompt; the
        # read module's own values make the advertised budget the enforced one.
        from swcraft_tools import query as query_module
        from swcraft_tools.editing import protocol as protocol_module
        from swcraft_tools.editing import read as read_module

        structured, _texts = call("discover")
        assert structured["reads"] == {
            "forms": list(read_module.READ_FORMS),
            "default_form": dict(read_module.DEFAULT_FORM_BY_SHAPE),
            "budget_wire_bytes": read_module.RESPONSE_BUDGET_WIRE_BYTES,
            "hint_page_size": read_module.HINT_PAGE_SIZE,
            # `discover` is refused against the same budget. Its `default_form` is the channel's,
            # `baseline`, where the protocol's own default is `summary`.
            "discover": {
                "forms": list(protocol_module.DISCOVER_FORMS),
                "default_form": HOST_DISCOVER_DEFAULT,
            },
            # What a read's `derived` may name.
            "derived": [entry.as_json() for entry in DERIVED],
            # The most rows a join's product may hold, and the most entries one `join` may carry.
            "join": {"row_ceiling": read_module.JOIN_ROW_CEILING, "max_entries": query_module.JOIN_MAX_ENTRIES},
        }

    def test_the_narrowed_discovery_carries_the_same_reads_block(self) -> None:
        assert call("discover", layer="swc").structured["reads"] == call("discover").structured["reads"]

    def test_the_omitted_layer_argument_is_not_sent_as_null(self) -> None:
        # `null` is not a `layerName`: forwarding the default would make "the whole project" a
        # schema violation.
        assert call("discover").structured == call("discover", layer=None).structured


class TestTheDiscoverForm:
    """The ``form`` of ``discover``, at the surface a client reads."""

    def test_the_host_default_over_the_wire_is_the_baseline(self) -> None:
        # A project-wide `discover` naming no form is the passport, the `reads` block and the
        # token: the agent host's profile carries the schema the descriptors would repeat.
        assert HOST_DISCOVER_DEFAULT == "baseline"
        structured, _texts = call("discover")
        assert structured == _engine({"operation": "discover"}, discover_default="baseline")
        assert structured["form"] == "baseline"
        assert structured["layers"] == []
        assert structured["reads"]["discover"]["default_form"] == "baseline"

    def test_a_layer_over_the_wire_is_still_the_summary(self) -> None:
        # A client naming one layer asks for its descriptor, so the layer keeps the protocol's
        # default while the `reads` block publishes the channel's.
        structured, _texts = call("discover", layer="swc")
        assert structured == _engine({"operation": "discover", "layer": "swc"}, discover_default="baseline")
        assert structured["form"] == "summary"
        assert [layer["layer"] for layer in structured["layers"]] == ["swc"]
        assert structured["reads"]["discover"]["default_form"] == "baseline"
        direct = _engine({"operation": "discover", "layer": "swc"})
        assert direct["form"] == "summary"
        assert direct["layers"] == structured["layers"]

    def test_a_direct_engine_call_with_no_keyword_answers_and_publishes_the_summary(self) -> None:
        # Callers without the keyword (the CLI, the vectors' replay) get the protocol's default.
        from swcraft_tools.editing import protocol as protocol_module

        direct = _engine({"operation": "discover"})
        assert direct["form"] == protocol_module.DEFAULT_DISCOVER_FORM == "summary"
        assert direct["reads"]["discover"]["default_form"] == "summary"
        assert len(direct["layers"]) == 15

    @pytest.mark.parametrize("form", ["summary", "full", "baseline"])
    def test_each_declared_form_reaches_the_engine_and_is_echoed(self, form: str) -> None:
        structured, _texts = call("discover", form=form)
        assert structured == _engine({"operation": "discover", "form": form}, HOST_DISCOVER_DEFAULT)
        if form == "full":
            # The project-wide `full` is always an addressless `over-budget`, forwarded as is; one
            # layer's `full` answers and echoes the form.
            assert structured["response"] == "error" and structured["error"] == "over-budget"
            assert "addresses" not in structured and "form" not in structured
            narrowed, _texts = call("discover", layer="swc", form="full")
            assert narrowed == _engine({"operation": "discover", "layer": "swc", "form": "full"}, HOST_DISCOVER_DEFAULT)
            assert narrowed["form"] == "full"
            return
        assert structured["form"] == form

    def test_the_full_form_carries_the_prose_the_default_strips(self) -> None:
        # "The prose is gone" is a claim about the wire. One layer, because the project-wide
        # `full` is a refusal with no layers to walk.
        summary, _ = call("discover", layer="swc")
        full, _ = call("discover", layer="swc", form="full")
        assert "description" not in _walk_keys(summary["layers"])
        assert "description" in _walk_keys(full["layers"])

    def test_the_init_field_reaches_the_wire_in_both_forms_and_its_prose_in_one(self) -> None:
        """The `init` descriptor a client needs to author one: `type: value`, optional, its prose in `full` alone.

        `summary` omits `required: false` and `immutable: false` (absent means `false`); `full`
        spells both out, with the sentence that the shape follows the declared type.

        What it does not reach: whether a value a client sends conforms; `swcraft check-model`'s
        `rule:interface.init-*` rules and the change set's validation passes are that gate.
        """
        summary, _ = call("discover", layer="interfaces")
        full, _ = call("discover", layer="interfaces", form="full")

        def _init_of(response: dict[str, Any]) -> dict[str, Any]:
            layer = next(entry for entry in response["layers"] if entry["layer"] == "interfaces")
            kind = next(entry for entry in layer["element_kinds"] if entry["kind"] == "data_element")
            return next(field for field in kind["fields"] if field["name"] == "init")

        assert _init_of(summary) == {"name": "init", "type": "value"}
        described = _init_of(full)
        assert described["type"] == "value" and described["required"] is False and described["immutable"] is False
        assert "declared type" in described["description"]

    def test_the_form_and_the_layer_compose_in_one_call(self) -> None:
        structured, _texts = call("discover", layer="hardware", form="full")
        assert structured == _engine(
            {"operation": "discover", "layer": "hardware", "form": "full"}, HOST_DISCOVER_DEFAULT
        )
        assert [layer["layer"] for layer in structured["layers"]] == ["hardware"]
        assert structured["form"] == "full"

    def test_the_omitted_form_argument_is_not_sent_as_null(self) -> None:
        # The schema refuses `form: null`, so the tool drops the key rather than forwarding it.
        assert call("discover").structured == call("discover", form=None).structured

    def test_an_undeclared_form_is_the_protocols_schema_violation(self) -> None:
        # `index` is a read form: the closest wrong answer, which a shared enum would admit.
        structured, _texts = call("discover", form="index")
        assert structured["response"] == "error"
        assert structured["error"] == "schema-violation"

    def test_a_non_string_form_is_a_tool_error(self) -> None:
        # Outside the declared type, so FastMCP refuses before the engine sees anything.
        with pytest.raises(ToolError):
            call("discover", form=1)


class TestNoStorageRootLeavesTheSurface:
    """No storage root leaves the surface a client reads."""

    def test_discovery_publishes_no_storage_root_and_no_model_hash(self, tmp_path: Path) -> None:
        structured, texts = call("discover")
        assert not (_walk_keys(structured) & FORBIDDEN_KEYS)
        rendered = json.dumps(structured) + "".join(texts)
        assert str(tmp_path) not in rendered
        assert "swcraft.yaml" not in rendered

    def test_the_narrowed_form_publishes_no_storage_root_either(self, tmp_path: Path) -> None:
        structured, texts = call("discover", layer="swc")
        assert not (_walk_keys(structured) & FORBIDDEN_KEYS)
        assert str(tmp_path) not in json.dumps(structured) + "".join(texts)


class TestRead:
    def test_reading_a_whole_layer_is_the_engines_answer(self, model_root: str) -> None:
        address = {"layer": "implementation_types"}
        structured, _texts = call("read", address=address)
        assert structured == _engine({"operation": "read", "address": address})
        assert structured["response"] == "read"
        assert structured["address"] == address
        # The base type is read at its own layer, through the same tool.
        assert [element["name"] for element in structured["value"]] == ["float32"]
        base_address = {"layer": "base_types"}
        base_structured, _texts = call("read", address=base_address)
        assert base_structured == _engine({"operation": "read", "address": base_address})
        assert [element["name"] for element in base_structured["value"]] == ["float32_base"]

    def test_reading_one_element_answers_with_that_element(self, model_root: str) -> None:
        address = {"layer": "swc", "element": "SpeedSensorSWC"}
        structured, _texts = call("read", address=address)
        assert structured == _engine({"operation": "read", "address": address})
        assert structured["value"]["name"] == "SpeedSensorSWC"

    def test_reading_one_field_walks_the_path(self, model_root: str) -> None:
        address = {
            "layer": "swc",
            "element": "SpeedSensorSWC",
            "path": [{"field": "ports"}, {"member": "pSpeed"}, {"field": "interface"}],
        }
        structured, _texts = call("read", address=address)
        assert structured == _engine({"operation": "read", "address": address})
        assert structured["value"] == "ISpeed"

    def test_an_absent_element_is_broken_reference(self, model_root: str) -> None:
        address = {"layer": "swc", "element": "NoSuchSWC"}
        structured, _texts = call("read", address=address)
        assert structured == _engine({"operation": "read", "address": address})
        assert structured["response"] == "error"
        assert structured["error"] == "broken-reference"

    def test_an_undeclared_field_is_unknown_field(self, model_root: str) -> None:
        address = {"layer": "swc", "element": "SpeedSensorSWC", "path": [{"field": "safety_level"}]}
        structured, _texts = call("read", address=address)
        assert structured["response"] == "error"
        assert structured["error"] == "unknown-field"

    @pytest.mark.parametrize(
        ("address", "why"),
        [
            ({"element": "SpeedSensorSWC"}, "no layer"),
            ({"layer": "swc", "element": "../../etc/passwd"}, "a traversal identity"),
            ({"layer": "swc", "element": "SpeedSensorSWC", "path": [{"field": 7}]}, "a non-string field step"),
            ({"layer": "swc", "storage": "swc/example/SpeedSensorSWC.yaml"}, "a storage path"),
        ],
    )
    def test_a_malformed_address_is_a_schema_violation(self, model_root: str, address: dict, why: str) -> None:
        # Malformed nested content binds as a `dict` and comes back as the protocol's rejection,
        # not a transport error.
        structured, _texts = call("read", address=address)
        assert structured == _engine({"operation": "read", "address": address})
        assert structured["response"] == "error", why
        assert structured["error"] == "schema-violation", why


class TestTheReadForm:
    """The ``form`` argument over the wire: taken, never sent as ``null``, a bad value refused by the protocol."""

    @pytest.mark.parametrize("form", ["summary", "full", "index"])
    def test_the_tool_accepts_each_declared_form(self, model_root: str, form: str) -> None:
        address = {"layer": "swc"}
        structured, _texts = call("read", address=address, form=form)
        assert structured == _engine({"operation": "read", "address": address, "form": form})
        assert structured["form"] == form

    def test_the_omitted_form_argument_is_not_sent_as_null(self, model_root: str) -> None:
        # `null` is not a `readForm`: forwarding it would make "let the address choose" a violation.
        address = {"layer": "swc"}
        assert call("read", address=address).structured == call("read", address=address, form=None).structured

    def test_the_tool_answers_a_null_form_that_the_protocol_document_refuses(self, model_root: str) -> None:
        """The tool reads ``form: null`` as "no form named"; the protocol document keeps refusing it.

        Both halves are asserted: one alone would let either the protocol's rule relax or the tool
        start forwarding ``null``.
        """
        address = {"layer": "swc"}
        answered = call("read", address=address, form=None).structured
        assert answered["response"] == "read"
        assert answered["form"] == "summary", "the address chose, exactly as an omitted argument would"

        refused = _engine({"operation": "read", "address": address, "form": None})
        assert refused["response"] == "error"
        assert refused["error"] == "schema-violation"
        assert refused["failure_class"] == "invalid"

    def test_an_explicitly_null_layer_is_discovers_no_layer_named(self, model_root: str) -> None:
        # `discover`'s sibling case, so the pair cannot drift apart.
        assert call("discover").structured == call("discover", layer=None).structured
        refused = _engine({"operation": "discover", "layer": None})
        assert refused["error"] == "schema-violation"

    def test_the_omitted_form_answers_the_addresss_default_and_says_so(self, model_root: str) -> None:
        assert call("read", address={"layer": "swc"}).structured["form"] == "summary"
        assert call("read", address={"layer": "swc", "element": "SpeedSensorSWC"}).structured["form"] == "full"

    def test_a_layer_summary_is_narrower_than_its_full_answer(self, model_root: str) -> None:
        address = {"layer": "swc"}
        summary = call("read", address=address, form="summary").structured["value"]
        full = call("read", address=address, form="full").structured["value"]
        assert [element["name"] for element in summary] == [element["name"] for element in full]
        assert all(set(narrow) < set(wide) for narrow, wide in zip(summary, full, strict=True))

    @pytest.mark.parametrize("form", ["summarised", "FULL", "", "index "])
    def test_an_undeclared_form_is_a_schema_violation(self, model_root: str, form: str) -> None:
        # A `str` argument, so pass A refuses it and the client reads the protocol's rejection
        # rather than a transport error it cannot act on.
        address = {"layer": "swc"}
        structured, _texts = call("read", address=address, form=form)
        assert structured == _engine({"operation": "read", "address": address, "form": form})
        assert structured["response"] == "error"
        assert structured["error"] == "schema-violation"
        assert structured["failure_class"] == "invalid"

    def test_a_non_string_form_is_a_tool_error(self) -> None:

        with pytest.raises(ToolError):
            call("read", address={"layer": "swc"}, form=["summary"])


class TestTheReadFilter:
    """The ``where`` argument over the wire: passed through, never sent as ``null``, refused by the protocol."""

    #: The fixture's two components differ by identity and nothing else a scalar clause sees.
    ONE = [{"field": "name", "equals": "SpeedCtrlSWC"}]

    def test_a_filtered_read_is_the_engines_answer(self, model_root: str) -> None:
        address = {"layer": "swc"}
        structured, _texts = call("read", address=address, where=self.ONE)
        assert structured == _engine({"operation": "read", "address": address, "where": self.ONE})
        assert [element["name"] for element in structured["value"]] == ["SpeedCtrlSWC"]

    def test_the_response_echoes_the_filter_the_call_carried(self, model_root: str) -> None:
        structured, _texts = call("read", address={"layer": "swc"}, where=self.ONE)
        assert structured["where"] == self.ONE

    def test_the_omitted_where_argument_is_not_sent_as_null(self, model_root: str) -> None:
        # The schema refuses `where: null` and an empty array alike.
        address = {"layer": "swc"}
        assert call("read", address=address).structured == call("read", address=address, where=None).structured
        assert "where" not in call("read", address=address).structured

        refused = _engine({"operation": "read", "address": address, "where": None})
        assert refused["response"] == "error"
        assert refused["error"] == "schema-violation"

    def test_a_filter_and_a_form_compose_in_one_call(self, model_root: str) -> None:
        structured, _texts = call("read", address={"layer": "swc"}, form="index", where=self.ONE)
        assert structured["form"] == "index"
        assert structured["value"] == {"count": 1, "identities": ["SpeedCtrlSWC"]}

    def test_a_collection_address_filters_its_members_over_the_wire(self, model_root: str) -> None:
        address = {"layer": "swc", "element": "SpeedSensorSWC", "path": [{"field": "ports"}]}
        where = [{"field": "direction", "equals": "provide"}]
        structured, _texts = call("read", address=address, where=where)
        assert [member["name"] for member in structured["value"]] == ["pSpeed"]

    def test_an_undeclared_filter_field_is_the_protocols_unknown_field(self, model_root: str) -> None:
        structured, _texts = call("read", address={"layer": "swc"}, where=[{"field": "safety_level", "exists": True}])
        assert structured["response"] == "error"
        assert structured["error"] == "unknown-field"
        assert structured["failure_class"] == "invalid"

    def test_a_clause_the_envelope_refuses_is_a_schema_violation_and_not_a_tool_error(self, model_root: str) -> None:
        # Well typed for the tool and refused by `$defs/whereClause`.
        structured, _texts = call(
            "read", address={"layer": "swc"}, where=[{"field": "name", "equals": "x", "exists": True}]
        )
        assert structured["error"] == "schema-violation"

    def test_a_non_list_where_is_a_tool_error(self) -> None:
        with pytest.raises(ToolError):
            call("read", address={"layer": "swc"}, where="name=SpeedCtrlSWC")


class TestTheReadPage:
    """The ``limit``/``after`` arguments over the wire: passed through, never ``null``, refused by the protocol."""

    def test_a_paged_read_is_the_engines_answer(self, model_root: str) -> None:
        address = {"layer": "swc"}
        structured, _texts = call("read", address=address, form="index", limit=1)
        assert structured == _engine({"operation": "read", "address": address, "form": "index", "limit": 1})
        assert structured["value"] == {"count": 1, "identities": ["SpeedCtrlSWC"]}
        assert structured["page"] == {"total": 2, "returned": 1, "truncated": True, "next": "SpeedCtrlSWC"}

    def test_the_next_cursor_is_replayed_verbatim_as_after(self, model_root: str) -> None:
        # What the page publishes is what the next call sends back, with no re-composition.
        first, _texts = call("read", address={"layer": "swc"}, form="index", limit=1)
        second, _texts = call("read", address={"layer": "swc"}, form="index", limit=1, after=first["page"]["next"])
        assert second["value"] == {"count": 1, "identities": ["SpeedSensorSWC"]}
        assert second["page"] == {"total": 2, "returned": 1, "truncated": True}

    def test_the_omitted_page_arguments_are_not_sent_as_null(self, model_root: str) -> None:
        # The schema refuses `limit: null`.
        address = {"layer": "swc"}
        plain = call("read", address=address).structured
        assert plain == call("read", address=address, limit=None, after=None).structured
        assert "page" not in plain

        refused = _engine({"operation": "read", "address": address, "limit": None})
        assert refused["response"] == "error"
        assert refused["error"] == "schema-violation"

    def test_a_page_a_filter_and_a_form_compose_in_one_call(self, model_root: str) -> None:
        structured, _texts = call(
            "read",
            address={"layer": "swc"},
            form="index",
            where=[{"field": "type", "equals": "application"}],
            limit=1,
        )
        assert structured["form"] == "index"
        assert structured["where"] == [{"field": "type", "equals": "application"}]
        assert structured["page"]["total"] == 2
        assert structured["value"] == {"count": 1, "identities": ["SpeedCtrlSWC"]}

    def test_a_cursor_the_set_does_not_carry_is_the_protocols_broken_reference(self, model_root: str) -> None:
        structured, _texts = call("read", address={"layer": "swc"}, limit=1, after="NoSuchSWC")
        assert structured["response"] == "error"
        assert structured["error"] == "broken-reference"
        assert structured["failure_class"] == "invalid"

    def test_a_cursor_without_a_limit_is_a_schema_violation_and_not_a_tool_error(self, model_root: str) -> None:
        # Well typed for the tool and refused by `$defs/readRequest`'s `dependentRequired`.
        structured, _texts = call("read", address={"layer": "swc"}, after="SpeedCtrlSWC")
        assert structured["error"] == "schema-violation"

    def test_a_non_integer_limit_is_a_tool_error(self) -> None:
        with pytest.raises(ToolError):
            call("read", address={"layer": "swc"}, limit=["10"])


class TestTheReadMemberSlice:
    """The ``members`` argument over the wire: passed through, never ``null``, refused by the protocol."""

    def test_a_member_slice_is_the_engines_answer(self, model_root: str) -> None:
        address = {"layer": "swc"}
        structured, _texts = call("read", address=address, members="ports")
        assert structured == _engine({"operation": "read", "address": address, "members": "ports"})
        # The row says which port is whose, which the address vocabulary needs one read per element for.
        assert structured["value"] == [
            {
                "element": "SpeedCtrlSWC",
                # A slice's summary renders each collection as its size.
                "member": {
                    "name": "rSpeed",
                    "direction": "require",
                    "interface": "ISpeed",
                    "measures": 0,
                    "com_specs": 0,
                    "satisfies": 0,
                },
            },
            {
                "element": "SpeedSensorSWC",
                "member": {
                    "name": "pSpeed",
                    "direction": "provide",
                    "interface": "ISpeed",
                    "measures": 0,
                    "com_specs": 0,
                    "satisfies": 0,
                },
            },
        ]

    def test_the_response_echoes_the_slice_the_call_carried(self, model_root: str) -> None:
        structured, _texts = call("read", address={"layer": "swc"}, members="ports")
        assert structured["members"] == "ports"

    def test_every_port_on_one_interface_with_its_owners_is_one_call(self, model_root: str) -> None:
        structured, _texts = call(
            "read",
            address={"layer": "swc"},
            members="ports",
            where=[{"field": "interface", "equals": "ISpeed"}],
        )
        assert [row["element"] for row in structured["value"]] == ["SpeedCtrlSWC", "SpeedSensorSWC"]
        assert structured["where"] == [{"field": "interface", "equals": "ISpeed"}]

    def test_the_omitted_members_argument_is_not_sent_as_null(self, model_root: str) -> None:
        # The schema refuses `members: null`.
        address = {"layer": "swc"}
        plain = call("read", address=address).structured
        assert plain == call("read", address=address, members=None).structured
        assert "members" not in plain

        refused = _engine({"operation": "read", "address": address, "members": None})
        assert refused["response"] == "error"
        assert refused["error"] == "schema-violation"

    def test_a_slice_a_filter_a_page_and_a_form_compose_in_one_call(self, model_root: str) -> None:
        structured, _texts = call(
            "read",
            address={"layer": "swc"},
            members="ports",
            where=[{"field": "direction", "equals": "provide"}],
            form="index",
            limit=1,
        )
        assert structured["members"] == "ports"
        assert structured["value"] == {"count": 1, "identities": [{"element": "SpeedSensorSWC", "member": "pSpeed"}]}
        assert structured["page"] == {"total": 1, "returned": 1, "truncated": False}

    def test_the_row_identity_is_replayed_verbatim_as_after(self, model_root: str) -> None:
        # A slice cursor is an object, hence `after: str | dict`.
        first, _texts = call("read", address={"layer": "swc"}, members="ports", form="index", limit=1)
        cursor = first["page"]["next"]
        assert cursor == {"element": "SpeedCtrlSWC", "member": "rSpeed"}
        second, _texts = call("read", address={"layer": "swc"}, members="ports", form="index", limit=1, after=cursor)
        assert second["value"] == {"count": 1, "identities": [{"element": "SpeedSensorSWC", "member": "pSpeed"}]}

    def test_an_undeclared_collection_is_the_protocols_unknown_field(self, model_root: str) -> None:
        structured, _texts = call("read", address={"layer": "swc"}, members="connections")
        assert structured["response"] == "error"
        assert structured["error"] == "unknown-field"
        assert structured["failure_class"] == "invalid"

    def test_a_slice_off_a_layer_address_is_a_schema_violation_and_not_a_tool_error(self, model_root: str) -> None:
        # Well typed for the tool and refused by the engine.
        structured, _texts = call("read", address={"layer": "swc", "element": "SpeedSensorSWC"}, members="ports")
        assert structured["error"] == "schema-violation"

    def test_a_non_string_members_is_a_tool_error(self) -> None:
        with pytest.raises(ToolError):
            call("read", address={"layer": "swc"}, members=["ports"])


class TestTheReadDerivedValues:
    """The ``derived`` argument over the wire: passed through, never ``null``, refused by the protocol."""

    def test_a_derived_read_is_the_engines_answer(self, model_root: str) -> None:
        address = {"layer": "connectors"}
        structured, _texts = call("read", address=address, form="index", derived=["connection_ends"])
        request = {"operation": "read", "address": address, "form": "index", "derived": ["connection_ends"]}
        assert structured == _engine(request)
        assert structured["response"] == "read"
        assert structured["derived"] == ["connection_ends"]
        rows = structured["value"]["identities"]
        assert rows, "the project's connectors answered no row, so the equality is vacuous"
        assert all(set(row) == {"element", "derived"} for row in rows)

    def test_an_omitted_derived_is_not_sent_as_null(self, model_root: str) -> None:
        address = {"layer": "connectors"}
        assert (
            call("read", address=address, form="index").structured
            == call("read", address=address, form="index", derived=None).structured
        )

    def test_a_refused_name_is_the_protocols_rejection(self, model_root: str) -> None:
        structured, _texts = call("read", address={"layer": "swc"}, form="index", derived=["connection_ends"])
        assert structured["response"] == "error"
        assert structured["error"] == "schema-violation"
        assert structured["addresses"] == [{"layer": "swc"}]


class TestTheReadJoin:
    """The ``join`` argument over the wire: passed through, never ``null``, refused by the protocol."""

    JOIN = [
        {
            "with": {"address": {"layer": "connectors"}, "derived": ["connection_ends"]},
            "on": [{"left": "element", "right": "derived.connection_ends.provider.swc"}],
            "as": "connection",
        }
    ]

    def test_a_joined_read_is_the_engines_answer(self, model_root: str) -> None:
        address = {"layer": "swc"}
        structured, _texts = call("read", address=address, form="index", join=self.JOIN)
        request = {"operation": "read", "address": address, "form": "index", "join": self.JOIN}
        assert structured == _engine(request)
        assert structured["response"] == "read"
        assert structured["join"] == self.JOIN
        rows = structured["value"]["identities"]
        assert rows, "the project's components provided no connector, so the equality is vacuous"
        assert all(set(row) == {"element", "connection"} for row in rows)
        assert all(row["connection"]["derived"]["connection_ends"]["provider"]["swc"] == row["element"] for row in rows)

    def test_an_omitted_join_is_not_sent_as_null(self, model_root: str) -> None:
        address = {"layer": "swc"}
        assert (
            call("read", address=address, form="index").structured
            == call("read", address=address, form="index", join=None).structured
        )

    def test_a_path_outside_the_row_is_the_protocols_rejection(self, model_root: str) -> None:
        join = [{**self.JOIN[0], "on": [{"left": "member", "right": "element"}]}]
        structured, _texts = call("read", address={"layer": "swc"}, form="index", join=join)
        assert structured["response"] == "error"
        assert structured["error"] == "schema-violation"
        assert structured["addresses"] == [{"layer": "swc"}]


class TestTheThreeOperationsTogether:
    """A member path, a computed value and a join through the tool: the sender-receiver table.

    A writing and a reading runnable reach a nested collection. Each answer is held to the
    engine's, and the asserted rows say what it is, so two equal empty answers cannot pass.

    What it does not reach: the stub component, the delegation, the type mapping and the budget.
    """

    PILOT_JOIN = [
        {
            "with": {"address": {"layer": "connectors"}, "derived": ["connection_ends"]},
            "on": [
                {"left": "element", "right": "derived.connection_ends.provider.swc"},
                {"left": "member.port", "right": "derived.connection_ends.provider.port"},
            ],
            "as": "connection",
        },
        {
            "with": {"address": {"layer": "swc"}, "members": "runnables.reads"},
            "on": [
                {"left": "connection.derived.connection_ends.requester.swc", "right": "element"},
                {"left": "connection.derived.connection_ends.requester.port", "right": "member.port"},
                {"left": "member.element", "right": "member.element"},
            ],
            "as": "reader",
        },
    ]

    @pytest.fixture
    def pilot_root(self, model_root: str) -> str:
        """``model_root`` with one writing and one reading runnable."""
        import yaml
        from swcraft_tools.model_registry import production_registry

        for name, port, direction, access in (
            ("SpeedSensorSWC", "pSpeed", "provide", "writes"),
            ("SpeedCtrlSWC", "rSpeed", "require", "reads"),
        ):
            path = Path(model_root) / production_registry().compose_path("swc", name, {"group": "example"})
            document = {
                "name": name,
                "type": "application",
                "ports": [{"name": port, "direction": direction, "interface": "ISpeed"}],
                "runnables": [{"name": f"{name}_Run", access: [{"port": port, "element": "speed"}]}],
                "handle_termination_and_restart": "no_support",
            }
            path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
        return model_root

    def test_a_member_path_is_the_engines_answer_with_its_owner_chain(self, pilot_root: str) -> None:
        address = {"layer": "swc"}
        structured, _texts = call("read", address=address, members="runnables.writes", form="index")
        request = {"operation": "read", "address": address, "members": "runnables.writes", "form": "index"}
        assert structured == _engine(request)
        assert structured["members"] == "runnables.writes"
        assert structured["value"] == {
            "count": 1,
            "identities": [
                {
                    "element": "SpeedSensorSWC",
                    "through": ["SpeedSensorSWC_Run"],
                    "member": {"port": "pSpeed", "element": "speed"},
                }
            ],
        }

    def test_a_computed_value_on_the_path_is_the_engines_answer(self, pilot_root: str) -> None:
        address = {"layer": "swc"}
        arguments = {"members": "runnables.writes", "form": "index", "derived": ["base_type"]}
        structured, _texts = call("read", address=address, **arguments)
        assert structured == _engine({"operation": "read", "address": address, **arguments})
        [row] = structured["value"]["identities"]
        assert row["derived"] == {"base_type": {"implementation": "float32", "base": "float32_base"}}

    def test_the_pilot_table_is_one_joined_call(self, pilot_root: str) -> None:
        address = {"layer": "swc"}
        arguments = {
            "members": "runnables.writes",
            "form": "index",
            "derived": ["base_type"],
            "join": self.PILOT_JOIN,
        }
        structured, _texts = call("read", address=address, **arguments)
        assert structured == _engine({"operation": "read", "address": address, **arguments})
        [row] = structured["value"]["identities"]
        assert (row["element"], row["through"], row["member"]) == (
            "SpeedSensorSWC",
            ["SpeedSensorSWC_Run"],
            {"port": "pSpeed", "element": "speed"},
        )
        assert row["connection"]["derived"]["connection_ends"]["requester"] == {
            "swc": "SpeedCtrlSWC",
            "port": "rSpeed",
        }
        assert row["reader"]["through"] == ["SpeedCtrlSWC_Run"]

    def test_the_surface_is_thirteen_tools(self) -> None:
        # The three operations are parameters of `read`, never tools of their own.
        import asyncio

        from fastmcp import Client
        from swcraft_mcp.server import mcp

        async def names() -> list[str]:
            async with Client(mcp) as client:
                return [tool.name for tool in await client.list_tools()]

        listed = asyncio.run(names())
        assert len(listed) == 13
        assert "read" in listed


class TestTheOuterBoundaryOwnsInvocationErrors:
    """An argument FastMCP cannot bind never reaches ``handle_request``.

    It has no protocol response; fabricating an ``errorResult`` would promise a reply the wire
    does not carry.
    """

    def test_read_without_an_address_is_the_model_map(self) -> None:
        # A read with no address is a request of its own, the map.
        structured, _texts = call("read")
        assert structured["response"] == "map"

    def test_a_non_object_address_is_a_tool_error(self) -> None:
        with pytest.raises(ToolError):
            call("read", address="swc")

    def test_a_non_string_layer_is_a_tool_error(self) -> None:
        with pytest.raises(ToolError):
            call("discover", layer=["swc"])


class TestNeitherOperationWritesAnything:
    """The read operations take no scope, leave no audit record and move no model byte."""

    def test_discover_and_read_leave_the_audit_log_empty(self, tmp_path: Path, model_root: str) -> None:
        before = audit_records(tmp_path)
        call("discover")
        call("discover", layer="swc")
        call("discover", layer="nosuchlayer")
        call("read", address={"layer": "swc"})
        call("read", address={"layer": "swc", "element": "NoSuchSWC"})
        assert audit_records(tmp_path) == before == []

    def test_the_model_is_byte_identical_afterwards(self, tmp_path: Path, model_root: str) -> None:
        root = Path(model_root)
        before = {path: path.read_bytes() for path in sorted(root.rglob("*.yaml"), key=lambda path: path.as_posix())}
        call("discover")
        call("read", address={"layer": "swc", "element": "SpeedSensorSWC"})
        assert {
            path: path.read_bytes() for path in sorted(root.rglob("*.yaml"), key=lambda path: path.as_posix())
        } == before

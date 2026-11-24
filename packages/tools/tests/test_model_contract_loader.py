"""The production contract loader, ``swcraft_tools.contract``.

Three properties carry the weight:

* **The loader and the test fixtures see the same bytes.**
  ``packages/tools/tests/support/model_contract_fixtures.py`` resolves the contract from
  ``conftest.REPO_ROOT``; the production loader goes through ``importlib.resources``, as
  ``cli/init_command.py::_template_file_plan`` reaches the project template. Comparing the two
  canonically serialized is therefore a real assertion about this checkout's install. The
  installed-wheel half lives in ``test_model_contract_packaging.py`` (``artifact``), because
  building a wheel is not a fast-gate cost.

* **An unknown major is refused before the document is parsed at all**, never partially read: a
  consumer that got as far as ``layers`` would build a registry from a truncated model. The
  version is screened on the raw text, and a spy on ``json.loads`` proves nothing was parsed.

* **The meta-schema is enforced when it can be, the structural check always.** ``jsonschema`` is
  a ``dev`` extra, so an installed product may not have it. The structural check -- the top-level
  keys the shipped schema's own ``required`` list names -- does **not** reach field-level shapes:
  a contract with all four top-level keys and a malformed layer passes it and is caught only by
  the meta-schema. Both branches are exercised.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import model_contract_fixtures as fixtures
import pytest
from swcraft_tools import contract


@pytest.fixture(autouse=True)
def _clean_loader_cache() -> Any:
    """No case may inherit or leave a cached read, schema or validator."""
    contract.clear_cache()
    yield
    contract.clear_cache()


def _canonical(document: Any) -> str:
    return json.dumps(document, sort_keys=True, ensure_ascii=False)


class _SpyJson:
    """A stand-in for the ``json`` module that counts ``loads`` calls."""

    def __init__(self) -> None:
        self.loads_calls = 0

    def loads(self, text: str) -> Any:
        self.loads_calls += 1
        return json.loads(text)

    def dumps(self, *args: Any, **kwargs: Any) -> str:
        return json.dumps(*args, **kwargs)


# ---------------------------------------------------------------------------
# The loader reads the shipped bytes
# ---------------------------------------------------------------------------


def test_the_loader_and_the_fixtures_read_the_same_contract() -> None:
    assert _canonical(contract.load_contract()) == _canonical(fixtures.load_contract())


def test_the_loader_and_the_fixtures_read_the_same_binding() -> None:
    assert _canonical(contract.load_binding()) == _canonical(fixtures.load_binding())


def test_the_loader_and_the_fixtures_read_the_same_schemas() -> None:
    """The same bytes, read through the two readers.

    The production side reads the resource directly: production hands a raw schema to nobody.
    """
    assert _canonical(json.loads(contract._read_resource("contract.schema.json"))) == _canonical(
        fixtures.load_meta_schema()
    )
    assert _canonical(json.loads(contract._read_resource("binding.schema.json"))) == _canonical(
        fixtures.load_binding_schema()
    )


def test_each_call_hands_back_an_independent_document() -> None:
    """The cache holds text, never a parsed document, so no consumer can mutate another's."""
    first = contract.load_contract()
    first["layers"] = []

    assert contract.load_contract()["layers"] != []


def test_the_contract_version_is_the_shipped_one() -> None:
    assert contract.contract_version() == contract.load_contract()["contractVersion"]


def test_the_supported_majors_are_the_ones_the_fixtures_use() -> None:
    """One declaration of the supported set, not two."""
    # One major: one contract document ships.
    assert contract.SUPPORTED_CONTRACT_MAJORS == frozenset({12})
    assert fixtures.SUPPORTED_CONTRACT_MAJORS is contract.SUPPORTED_CONTRACT_MAJORS
    assert fixtures.UnsupportedContractVersion is contract.UnsupportedContractVersion


# ---------------------------------------------------------------------------
# The version guard
# ---------------------------------------------------------------------------


#: The genuine reader, captured before any case replaces the module attribute: a doctored case
#: still needs the real schema bytes.
_REAL_READ = contract._read_resource


def _real(name: str) -> str:
    """The genuine resource text, for the schema reads a doctored case still needs."""
    return _REAL_READ(name)


def _serve(monkeypatch: pytest.MonkeyPatch, document: Any) -> None:
    """Make the loader read *document* as ``model-contract.json``."""
    payload = json.dumps(document)

    def read(name: str) -> str:
        return payload if name == "model-contract.json" else _real(name)

    monkeypatch.setattr(contract, "_read_resource", read)


def test_an_unknown_major_is_refused_before_anything_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The message names the major and the supported set; ``json.loads`` never runs."""
    spy = _SpyJson()
    _serve(monkeypatch, {"contractVersion": "13.0.0", "swcraftVersion": "0.2.0", "layers": [], "rules": []})
    monkeypatch.setattr(contract, "json", spy)

    with pytest.raises(contract.UnsupportedContractVersion) as excinfo:
        contract.load_contract()

    message = str(excinfo.value)
    assert "13.0.0" in message
    assert "major 13" in message
    assert "[12]" in message
    assert spy.loads_calls == 0, "the document was parsed before its major was checked"


def test_a_superseded_major_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A superseded `3.x` major is refused exactly as an unknown future one is.

    The supported set is one entry and not a floor. `3.4.0` because it is what `0.8.0` shipped.

    **What this does not reach:** a migration; there is no `3.x` reader.
    """
    _serve(monkeypatch, {"contractVersion": "3.4.0", "swcraftVersion": "0.8.0", "layers": [], "rules": []})

    with pytest.raises(contract.UnsupportedContractVersion) as excinfo:
        contract.load_contract()

    message = str(excinfo.value)
    assert "3.4.0" in message
    assert "major 3" in message
    assert "[12]" in message, "the refusal must name the supported set, not only the major it refused"


def test_the_released_major_this_repository_read_until_contract_5_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `4.x` document (what `0.10.1` shipped) is refused, naming the supported set.

    **What this does not reach:** a migration; there is no `4.x` reader.
    """
    _serve(monkeypatch, {"contractVersion": "4.7.1", "swcraftVersion": "0.10.1", "layers": [], "rules": []})

    with pytest.raises(contract.UnsupportedContractVersion) as excinfo:
        contract.load_contract()

    message = str(excinfo.value)
    assert "4.7.1" in message
    assert "major 4" in message
    assert "[12]" in message, "the refusal must name the supported set, not only the major it refused"


def test_the_released_major_this_repository_read_until_contract_6_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `5.x` document (what `0.11.0` shipped) is refused, naming the supported set.

    **What this does not reach:** a migration; there is no `5.x` reader.
    """
    _serve(monkeypatch, {"contractVersion": "5.0.0", "swcraftVersion": "0.11.0", "layers": [], "rules": []})

    with pytest.raises(contract.UnsupportedContractVersion) as excinfo:
        contract.load_contract()

    message = str(excinfo.value)
    assert "5.0.0" in message
    assert "major 5" in message
    assert "[12]" in message, "the refusal must name the supported set, not only the major it refused"


def test_the_released_major_this_repository_read_until_contract_7_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `6.x` document (what `0.12.0` shipped) is refused, naming the supported set.

    **What this does not reach:** a migration; there is no `6.x` reader.
    """
    _serve(monkeypatch, {"contractVersion": "6.0.0", "swcraftVersion": "0.12.0", "layers": [], "rules": []})

    with pytest.raises(contract.UnsupportedContractVersion) as excinfo:
        contract.load_contract()

    message = str(excinfo.value)
    assert "6.0.0" in message
    assert "major 6" in message
    assert "[12]" in message, "the refusal must name the supported set, not only the major it refused"


def test_the_released_major_this_repository_read_until_contract_8_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `7.x` document is refused, naming the supported set.

    **What this does not reach:** a migration; there is no `7.x` reader.
    """
    _serve(monkeypatch, {"contractVersion": "7.11.0", "swcraftVersion": "0.13.0", "layers": [], "rules": []})

    with pytest.raises(contract.UnsupportedContractVersion) as excinfo:
        contract.load_contract()

    message = str(excinfo.value)
    assert "7.11.0" in message
    assert "major 7" in message
    assert "[12]" in message, "the refusal must name the supported set, not only the major it refused"


def test_the_released_major_this_repository_read_until_contract_9_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An `8.x` document (what `0.14.0` shipped) is refused, naming the supported set.

    **What this does not reach:** a migration; there is no `8.x` reader.
    """
    _serve(monkeypatch, {"contractVersion": "8.0.0", "swcraftVersion": "0.14.0", "layers": [], "rules": []})

    with pytest.raises(contract.UnsupportedContractVersion) as excinfo:
        contract.load_contract()

    message = str(excinfo.value)
    assert "8.0.0" in message
    assert "major 8" in message
    assert "[12]" in message, "the refusal must name the supported set, not only the major it refused"


def test_the_released_major_this_repository_read_until_contract_10_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `9.x` document (what `0.15.0` shipped) is refused, naming the supported set.

    **What this does not reach:** a migration; there is no `9.x` reader.
    """
    _serve(monkeypatch, {"contractVersion": "9.2.0", "swcraftVersion": "0.15.0", "layers": [], "rules": []})

    with pytest.raises(contract.UnsupportedContractVersion) as excinfo:
        contract.load_contract()

    message = str(excinfo.value)
    assert "9.2.0" in message
    assert "major 9" in message
    assert "[12]" in message, "the refusal must name the supported set, not only the major it refused"


@pytest.mark.parametrize(
    "declared",
    ["1.abc", "01.0.0", "1.0", "", "v1.0.0", "1.0.0-rc1"],
    ids=lambda value: value or "empty",
)
def test_a_version_that_is_not_semantic_is_refused(monkeypatch: pytest.MonkeyPatch, declared: str) -> None:
    _serve(monkeypatch, {"contractVersion": declared, "swcraftVersion": "0.2.0", "layers": [], "rules": []})

    with pytest.raises(contract.UnsupportedContractVersion, match="not a semantic version"):
        contract.load_contract()


def test_a_document_declaring_no_version_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """No default: an unversioned contract must not read as the newest one."""
    _serve(monkeypatch, {"swcraftVersion": "0.2.0", "layers": [], "rules": []})

    with pytest.raises(contract.UnsupportedContractVersion, match="declares no contractVersion"):
        contract.load_contract()


#: Documents both guards must refuse. The text guard runs on the serialized form before
#: ``json.loads``, the document guard on the parsed form: two implementations of one rule,
#: pinned against each other.
_REFUSED_DOCUMENTS = [
    {},
    {"contractVersion": "13.0.0"},
    # Every superseded major this repository once read. The supported set is one entry, not a
    # floor, so each stays listed as the major moves rather than the oldest being retired.
    {"contractVersion": "11.1.0"},
    {"contractVersion": "10.2.0"},
    {"contractVersion": "9.2.0"},
    {"contractVersion": "8.0.0"},
    {"contractVersion": "7.11.0"},
    {"contractVersion": "6.0.0"},
    {"contractVersion": "5.1.0"},
    {"contractVersion": "4.7.1"},
    {"contractVersion": "3.4.0"},
    {"contractVersion": "2.0.1"},
    {"contractVersion": "1.4.3"},
    {"contractVersion": "0.9.0"},
    {"contractVersion": "1.abc"},
    {"contractVersion": "01.0.0"},
    {"contractVersion": 2},
    {"contractVersion": None},
]


@pytest.mark.parametrize("document", _REFUSED_DOCUMENTS, ids=lambda doc: repr(doc.get("contractVersion", "absent")))
def test_both_version_guards_refuse_the_same_documents(monkeypatch: pytest.MonkeyPatch, document: Any) -> None:
    with pytest.raises(contract.UnsupportedContractVersion):
        contract.require_supported_contract_version(document)

    _serve(monkeypatch, document)
    with pytest.raises(contract.UnsupportedContractVersion):
        contract.load_contract()


def test_the_document_guard_accepts_the_shipped_contract() -> None:
    contract.require_supported_contract_version(contract.load_contract())


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["swcraftVersion", "layers", "rules"])
def test_a_missing_top_level_key_is_a_shape_error(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    document = {"contractVersion": "12.0.0", "swcraftVersion": "0.2.0", "layers": [], "rules": []}
    del document[missing]
    _serve(monkeypatch, document)

    with pytest.raises(contract.ContractShapeError, match=missing):
        contract.load_contract()


def test_a_binding_missing_a_top_level_key_is_a_shape_error(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = contract.load_binding()
    del binding["snapshotAttributes"]
    payload = json.dumps(binding)
    monkeypatch.setattr(contract, "_read_resource", lambda name: payload if name == "binding.json" else _real(name))

    with pytest.raises(contract.ContractShapeError, match="snapshotAttributes"):
        contract.load_binding()


def test_a_document_that_is_not_an_object_is_a_shape_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A supported version inside a document that is not an object is a shape error.

    The version is spelled so that the raw-text screen passes and the shape check is reached.
    """
    monkeypatch.setattr(
        contract,
        "_read_resource",
        lambda name: '[{"contractVersion": "12.0.0"}]' if name == "model-contract.json" else _real(name),
    )

    with pytest.raises(contract.ContractShapeError, match="not a JSON object"):
        contract.load_contract()


def test_the_shipped_contract_carries_exactly_one_contract_version_occurrence() -> None:
    """The shipped contract names `contractVersion` once, so `contract_version()` is authoritative.

    It answers the first occurrence in the raw text, so a second one could disagree with
    `load_contract()`.
    """
    text = _real("model-contract.json")

    assert text.count('"contractVersion"') == 1
    assert contract.contract_version() == contract.load_contract()["contractVersion"]


def test_a_schema_declaring_no_top_level_required_list_is_a_shape_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A schema without its `required` list is refused, not a structural check reduced to nothing."""
    schema = json.loads(_real("contract.schema.json"))
    del schema["required"]
    payload = json.dumps(schema)
    monkeypatch.setattr(
        contract,
        "_read_resource",
        lambda name: payload if name == "contract.schema.json" else _real(name),
    )
    contract._required_keys.cache_clear()

    with pytest.raises(contract.ContractShapeError, match="declares no top-level `required` key list"):
        contract.load_contract()

    contract._required_keys.cache_clear()


def test_malformed_json_is_a_shape_error_naming_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        contract,
        "_read_resource",
        lambda name: '{"contractVersion": "12.0.0",' if name == "model-contract.json" else _real(name),
    )

    with pytest.raises(contract.ContractShapeError, match="model-contract.json"):
        contract.load_contract()


# ---------------------------------------------------------------------------
# The meta-schema, when it can be enforced
# ---------------------------------------------------------------------------


def test_jsonschema_is_importable_in_this_checkout() -> None:
    """The precondition of the enforcement case."""
    assert contract._import_validator_class() is not None


def test_the_validator_is_absent_when_jsonschema_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real ``ImportError`` branch: ``None`` in ``sys.modules`` masks the module."""
    monkeypatch.setitem(sys.modules, "jsonschema", None)

    assert contract._import_validator_class() is None


def _structurally_complete_but_invalid() -> dict[str, Any]:
    """All four top-level keys, one layer the meta-schema refuses."""
    return {
        "contractVersion": "12.0.0",
        "swcraftVersion": "0.2.0",
        "layers": [{"id": "layer:bogus"}],
        "rules": [],
    }


def test_the_meta_schema_is_enforced_when_jsonschema_is_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _structurally_complete_but_invalid())

    with pytest.raises(contract.ContractShapeError, match="meta-schema"):
        contract.load_contract()


def test_the_structural_check_still_runs_without_jsonschema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the extra, the top-level keys are still required, and only those."""
    monkeypatch.setattr(contract, "_import_validator_class", lambda: None)
    _serve(monkeypatch, _structurally_complete_but_invalid())

    assert contract.load_contract()["layers"] == [{"id": "layer:bogus"}]

    _serve(monkeypatch, {"contractVersion": "12.0.0", "swcraftVersion": "0.2.0", "layers": []})
    with pytest.raises(contract.ContractShapeError, match="rules"):
        contract.load_contract()


def test_the_shipped_binding_satisfies_its_meta_schema() -> None:
    """Loading it is the assertion — ``load_binding`` validates on the way out."""
    assert set(contract.load_binding()) >= {"kinds", "rules"}


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


class _CountingResources:
    """A ``resource_files`` stand-in that counts how often it is consulted."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.calls = 0

    def __call__(self, package: str) -> Any:
        self.calls += 1
        return self._real(package)


def test_a_resource_is_read_once_until_the_cache_is_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = _CountingResources(contract.resource_files)
    monkeypatch.setattr(contract, "resource_files", counter)

    contract.load_contract()
    reads_after_first = counter.calls
    contract.load_contract()

    assert reads_after_first > 0, "the loader never reached the packaged resource"
    assert counter.calls == reads_after_first, "the second load re-read the resource"

    contract.clear_cache()
    contract.load_contract()

    assert counter.calls > reads_after_first, "clear_cache() left the stale text in place"


def test_clear_cache_also_drops_the_derived_schema_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """``clear_cache()`` drops the required-key tuple derived from the schema, not only the text."""
    document = {"contractVersion": "12.0.0", "swcraftVersion": "0.2.0", "layers": [], "rules": []}
    stricter = json.dumps({"required": ["contractVersion", "swcraftVersion", "layers", "rules", "extra"]})
    strict_schema = False

    def read(name: str) -> str:
        if name == "model-contract.json":
            return json.dumps(document)
        if name == "contract.schema.json" and strict_schema:
            return stricter
        return _real(name)

    monkeypatch.setattr(contract, "_import_validator_class", lambda: None)
    monkeypatch.setattr(contract, "_read_resource", read)

    contract.load_contract()  # warms the derived tuple from the real schema
    strict_schema = True

    assert contract.load_contract() == document, "the derived required-key tuple was not cached"

    contract.clear_cache()
    with pytest.raises(contract.ContractShapeError, match="extra"):
        contract.load_contract()

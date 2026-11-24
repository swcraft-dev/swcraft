"""The wire shapes of the editing protocol: its requests, responses, codes and classes.

The Python form of the protocol's JSON Schema: it declares the shapes and parses
them strictly, raising :class:`EnvelopeError`; it decides nothing.

**``failure_class`` is derived, never carried**, from :data:`ERROR_CLASS_MAP`;
parsing an ``errorResult`` whose ``failure_class`` contradicts the map is refused.

**What this module has no branch for**, each absence a security invariant:

* **no request field takes a file path, a directory, raw YAML or raw text.** The
  only client string that reaches a path component is an element ``identity``,
  held to a single segment;
* **no commit request and no transaction handle**: one change set produces
  exactly one response;
* **no force flag, no skip-validation parameter and no partial-commit mode.**
  Every shape is closed (``additionalProperties: false``).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar, Literal

# The join entry cap lives in the CLI parser's module (``query.request``), so both
# parsers refuse with one number and one sentence; it is read at the call, so
# lowering it there lowers both parsers and what ``discover`` publishes.
from swcraft_tools import query as _kernel_query
from swcraft_tools.editing.address import (
    LAYER_NAME_PATTERN,
    Address,
    AddressError,
)

#: The one reading of a JSON integer, the kernel's: ``swcraft query --request``
#: parses the same ``read`` shape, and both parsers must reach one verdict.
integral = _kernel_query.integral

__all__ = [
    "ABSENT",
    "ADDRESS_SHAPES",
    "BEFORE_FORMS",
    "CANDIDATES_PER_NAME",
    "CANDIDATE_TIERS",
    "CHANGE_OPERATION_NAMES",
    "CREATE_CARRIES_A_VALUE",
    "DEFAULT_DISCOVER_FORM",
    "DELTA_ORIGINS",
    "DISCOVER_FORMS",
    "ERROR_CLASS_MAP",
    "ERROR_CODES",
    "FAILURE_CLASSES",
    "FIND_CANDIDATE_BANDS",
    "FIND_CANDIDATES_DEFAULT",
    "FIND_CANDIDATES_MAX",
    "FIND_CANDIDATES_TOTAL",
    "FIND_EXISTING_LINKS_MAX",
    "FIND_FORMS",
    "FIND_MAX",
    "FIND_TEXT_MAX",
    "MODEL_MAP_LIST_MAX",
    "OVER_BUDGET",
    "READ_BATCH_MAX",
    "READ_FORMS",
    "REMOVE_CARRIES_NO_VALUE",
    "REQUEST_OPERATIONS",
    "REVISION_TOKEN_PATTERN",
    "UPDATE_CARRIES_A_VALUE",
    "AddressShape",
    "BeforeForm",
    "ChangeOperation",
    "ChangeSetRequest",
    "ChangeSetResponse",
    "ChangeSetStatus",
    "CountRequest",
    "CountResponse",
    "CreateOperation",
    "DeltaEntry",
    "DeltaOrigin",
    "DiscoverCapability",
    "DiscoverForm",
    "DiscoverRequest",
    "DiscoverResponse",
    "EnvelopeError",
    "ErrorCode",
    "ErrorResult",
    "FailureClass",
    "FindCandidatesResponse",
    "FindForRequirementRequest",
    "FindRequest",
    "FindResponse",
    "FindTextRequest",
    "FindTextResponse",
    "Hint",
    "JOIN_ALIAS_PATTERN",
    "MEMBERS_PATH_PATTERN",
    "RESERVED_JOIN_ALIASES",
    "JoinCapability",
    "JoinCondition",
    "JoinEntry",
    "JoinedRead",
    "Measured",
    "ModelMapRequest",
    "ModelMapResponse",
    "Page",
    "ProtocolError",
    "ReadForm",
    "ReadManyRequest",
    "ReadManyResponse",
    "ReadRequest",
    "ReadResponse",
    "ReadsCapability",
    "RemoveOperation",
    "Passport",
    "Request",
    "Response",
    "UpdateOperation",
    "Violation",
    "WhereClause",
    "candidates_per_name",
    "check_rows",
    "failure_class",
    "integral",
    "is_positive_int",
    "parse_change_operation",
    "parse_change_set_envelope",
    "parse_find_request",
    "parse_read_request",
    "parse_response",
    "request_operation",
]


# ---------------------------------------------------------------------------
# The closed enums and the checked denormalization
# ---------------------------------------------------------------------------

ErrorCode = Literal[
    "schema-violation",
    "unknown-field",
    "invalid-identifier",
    "broken-reference",
    "identity-conflict",
    "immutable-field",
    "stale-baseline",
    "unauthorized-delta",
    "partial-change-set",
    "consistency-violation",
    "over-budget",
]

FailureClass = Literal["invalid", "unauthorized", "stale", "partial"]

ChangeSetStatus = Literal["staged", "committed", "abandoned"]

ReadForm = Literal["summary", "full", "index"]

#: ``$defs/readForm``'s members, in the order ``discover`` publishes them. A
#: *narrowing* order — ``full`` then ``summary`` then ``index`` — is a different
#: statement and belongs to the refusal chain, not to the enum.
READ_FORMS: tuple[ReadForm, ...] = ("summary", "full", "index")

DiscoverForm = Literal["summary", "full", "baseline"]

DeltaOrigin = Literal["server", "client_and_server"]

#: ``$defs/deltaEntry``'s ``origin`` values: who stated a change the server
#: completed (``ChangeSetRequest.complete``).
DELTA_ORIGINS: tuple[DeltaOrigin, ...] = ("server", "client_and_server")

BeforeForm = Literal["summary"]

#: ``$defs/deltaEntry``'s ``before_form`` values: the one narrower form a
#: whole-element ``remove``'s ``before`` is shown in where the delta leaves the
#: engine. Absent means whole.
BEFORE_FORMS: tuple[BeforeForm, ...] = ("summary",)

#: ``$defs/discoverForm``'s members, in the order ``discover`` publishes them.
#: ``index`` is not one: a discovery has no set to index. ``baseline`` answers the
#: passport, the ``reads`` block and the token with no layer descriptor, for a
#: client that holds the schema already and needs only the token.
DISCOVER_FORMS: tuple[DiscoverForm, ...] = ("summary", "full", "baseline")

#: The form a ``discover`` answers in when the request names none. ``summary``
#: strips every descriptor ``description`` and leaves out ``false`` flags
#: (``required``/``immutable``/``optional``), because the prose dominates the
#: response size. A flat constant of the wire vocabulary, unlike a read's
#: address-dependent default, so it is declared here where both its readers can import it.
DEFAULT_DISCOVER_FORM: DiscoverForm = "summary"

#: The one ``$defs/errorCode`` that carries ``hints`` and ``measured``
#: (enforced by :meth:`Violation.__post_init__`).
OVER_BUDGET = "over-budget"

AddressShape = Literal["layer", "collection", "element", "field"]

#: The four address shapes ``reads.default_form`` publishes a default for;
#: :mod:`swcraft_tools.editing.read` decides what each defaults to.
ADDRESS_SHAPES: tuple[AddressShape, ...] = ("layer", "collection", "element", "field")

#: ``$defs/errorCode`` -> ``$defs/failureClass``. A copy of the schema's
#: ``error_class_map``, which is not shipped; a test pins the two equal.
ERROR_CLASS_MAP: Mapping[str, str] = MappingProxyType(
    {
        "schema-violation": "invalid",
        "unknown-field": "invalid",
        "invalid-identifier": "invalid",
        "broken-reference": "invalid",
        "identity-conflict": "invalid",
        "immutable-field": "invalid",
        "stale-baseline": "stale",
        "unauthorized-delta": "unauthorized",
        "partial-change-set": "partial",
        "consistency-violation": "invalid",
        "over-budget": "invalid",
    }
)

#: The closed code enum, in declaration order.
ERROR_CODES: tuple[str, ...] = tuple(ERROR_CLASS_MAP)

#: The four failure classes. Every one leaves the real model byte-identical:
#: a closed set, not a severity scale.
FAILURE_CLASSES: frozenset[str] = frozenset(ERROR_CLASS_MAP.values())

#: The four request discriminators of ``$defs/request``. There is no ``commit``
#: (commit is the host's act) and no ``rename`` (a remove plus a create), so
#: neither has a branch anywhere in this module.
REQUEST_OPERATIONS: tuple[str, ...] = ("discover", "read", "find", "change_set")

#: ``$defs/changeOperationName``.
CHANGE_OPERATION_NAMES: tuple[str, ...] = ("create", "update", "remove")

#: ``$defs/revisionToken``. Opaque: only the framing is checked, because
#: equality is the only operation on a token.
REVISION_TOKEN_PATTERN = re.compile(r"^rev[0-9]+:[A-Za-z0-9_-]{8,512}$")

_CHANGE_SET_STATUSES: frozenset[str] = frozenset({"staged", "committed", "abandoned"})

_ARXML_VERSIONS: frozenset[str] = frozenset({"4.2.2", "4.3.0"})


def failure_class(error: str) -> str:
    """The failure class of *error*, through :data:`ERROR_CLASS_MAP`.

    Raises:
        ValueError: *error* is not a declared code. There is no default class: an
            unknown code is an engine defect, and ``invalid`` would tell a client to
            fix a request that is fine.
    """
    try:
        return ERROR_CLASS_MAP[error]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"'{error}' is not one of the protocol error codes {list(ERROR_CODES)}") from exc


class EnvelopeError(ValueError):
    """The request is not one ``$defs/request`` admits.

    Every condition it carries is structural — a shape, a required key, a closed
    enum member, a pattern. Pass A maps it onto ``schema-violation``; it is
    never a semantic rejection, which is what ``unknown-field`` and
    ``broken-reference`` are for.
    """


# ---------------------------------------------------------------------------
# The rejection shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Measured:
    """``$defs/measured`` — how big the refused response was, against what.

    Carried by an ``over-budget`` violation and by nothing else.

    ``wire_bytes`` is what the host would have received, **measured** over the one
    text block the MCP server sends; JSON-RPC framing (about 70 bytes) is not
    counted. ``elements`` is how many things the refused response would have
    carried, or, for a ``join`` above ``reads.join.row_ceiling``, the rows the join
    would have built up to the refused entry; that refusal is decided by
    ``elements``, and ``budget`` is then for reference only.
    """

    wire_bytes: int
    budget: int
    elements: int

    def __post_init__(self) -> None:
        for name in ("wire_bytes", "budget", "elements"):
            value = getattr(self, name)
            number = integral(value, 0)
            if number is None:
                raise ValueError(f"`measured.{name}` is a non-negative integer, not {value!r}")
            object.__setattr__(self, name, number)

    def to_json(self) -> dict[str, Any]:
        return {"wire_bytes": self.wire_bytes, "budget": self.budget, "elements": self.elements}

    @classmethod
    def from_json(cls, data: Any) -> Measured:
        _object(data, "a measured block")
        keys = {"wire_bytes", "budget", "elements"}
        _closed_keys(data, keys, keys, "a measured block")
        try:
            return cls(wire_bytes=data["wire_bytes"], budget=data["budget"], elements=data["elements"])
        except ValueError as invalid:
            raise EnvelopeError(str(invalid)) from invalid


@dataclass(frozen=True)
class Violation:
    """One violation: a code, a message, and the address(es) responsible.

    ``failure_class`` is not a field: it is derived from ``error`` on the way out,
    so the wire value cannot contradict :data:`ERROR_CLASS_MAP`. ``addresses`` is
    empty by default (a malformed envelope and a stale baseline have none) and then
    omitted from the JSON rather than sent as ``[]``.
    """

    error: str
    message: str
    addresses: tuple[Address, ...] = ()
    #: The narrower requests the server would take instead, in the refusal
    #: chain's order: ``readRequest`` or ``discoverRequest`` items, never a change
    #: set. Only ``over-budget`` carries any; otherwise the key is off the wire.
    hints: tuple[Hint, ...] = ()
    #: The sizes behind an ``over-budget`` refusal, which always carries it (it
    #: stays actionable once ``hints`` is exhausted); ``None`` on every other code.
    measured: Measured | None = None

    def __post_init__(self) -> None:
        failure_class(self.error)  # refuses an undeclared code at construction
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("a violation carries a non-empty human-readable message (its text is not contract)")
        object.__setattr__(self, "addresses", tuple(self.addresses))
        object.__setattr__(self, "hints", tuple(self.hints))
        # `hints` and `measured` belong to `over-budget` alone, enforced at
        # construction rather than left to the dataclass defaults.
        if self.error != OVER_BUDGET and (self.hints or self.measured is not None):
            raise ValueError(f"`hints` and `measured` are carried by '{OVER_BUDGET}' alone, not by '{self.error}'")
        # And the other direction: `hints` may be absent (the chain is exhausted),
        # but `measured` is what is left then, so an `over-budget` without it is a dead end.
        if self.error == OVER_BUDGET and self.measured is None:
            raise ValueError(f"an '{OVER_BUDGET}' refusal carries `measured` — the size, the budget and the count")

    @property
    def failure_class(self) -> str:
        """The class of :attr:`error`, always through the map."""
        return failure_class(self.error)

    def to_json(self) -> dict[str, Any]:
        """``$defs/errorDetail``."""
        detail: dict[str, Any] = {
            "error": self.error,
            "failure_class": self.failure_class,
            "message": self.message,
        }
        if self.addresses:
            detail["addresses"] = [address.to_json() for address in self.addresses]
        if self.hints:
            detail["hints"] = [hint.to_json() for hint in self.hints]
        if self.measured is not None:
            detail["measured"] = self.measured.to_json()
        return detail

    @classmethod
    def from_json(cls, data: Any) -> Violation:
        _object(data, "an errorDetail")
        _closed_keys(
            data,
            {"error", "failure_class", "message", "addresses", "hints", "measured"},
            {"error", "failure_class", "message"},
            "an errorDetail",
        )
        error = data["error"]
        if not _is_declared(error, ERROR_CLASS_MAP):
            raise EnvelopeError(f"'{error}' is not one of the protocol error codes {list(ERROR_CODES)}")
        declared = data["failure_class"]
        if declared != ERROR_CLASS_MAP[error]:
            raise EnvelopeError(
                f"failure_class '{declared}' contradicts error_class_map: '{error}' is "
                f"'{ERROR_CLASS_MAP[error]}'. The map is the source of truth"
            )
        try:
            return cls(
                error=error,
                message=_non_empty_string(data["message"], "message"),
                addresses=_addresses(data),
                hints=_hints(data),
                measured=_measured(data),
            )
        except EnvelopeError:
            raise
        except ValueError as invalid:
            # `__post_init__`'s pairing guard, in either direction, reached from
            # the wire: the schema states both, so it is an envelope error.
            raise EnvelopeError(str(invalid)) from invalid


@dataclass(frozen=True)
class ErrorResult:
    """``$defs/errorResult`` — the one rejection shape, whatever the pass.

    The **primary** violation is the first one the pass order reaches and it is
    hoisted to the top level, so a client reading only the top level always
    reads a real violation. Every further violation found in that pass and in
    any later pass that still ran goes in ``additional``, in the order it was
    found; ``additional`` **never repeats the primary** and is **omitted rather
    than empty** when the primary is the only violation. Both rules are enforced
    here rather than left to each caller.
    """

    primary: Violation
    additional: tuple[Violation, ...] = ()

    def __post_init__(self) -> None:
        extra = tuple(self.additional)
        repeated = [violation for violation in extra if violation == self.primary]
        if repeated:
            raise ValueError("`additional` never repeats the primary violation")
        object.__setattr__(self, "additional", extra)

    @property
    def error(self) -> str:
        return self.primary.error

    @property
    def failure_class(self) -> str:
        """The class of the PRIMARY code; every class leaves the real model byte-identical."""
        return self.primary.failure_class

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {"response": "error"} | self.primary.to_json()
        if self.additional:
            result["additional"] = [violation.to_json() for violation in self.additional]
        return result

    @classmethod
    def from_json(cls, data: Any) -> ErrorResult:
        _object(data, "an errorResult")
        _closed_keys(
            data,
            {"response", "error", "failure_class", "message", "addresses", "hints", "measured", "additional"},
            {"response", "error", "failure_class", "message"},
            "an errorResult",
        )
        _const(data["response"], "error", "response")
        hoisted = {key: value for key, value in data.items() if key not in {"response", "additional"}}
        primary = Violation.from_json(hoisted)
        raw_additional = data.get("additional")
        if raw_additional is None:
            if "additional" in data:
                # `null` is not "omitted", as for `addresses` and `hints`.
                raise EnvelopeError("`additional` is an array of at least one errorDetail, or is omitted entirely")
            return cls(primary=primary)
        if not _is_array(raw_additional) or not raw_additional:
            raise EnvelopeError("`additional` is an array of at least one errorDetail, or is omitted entirely")
        return cls(primary=primary, additional=tuple(Violation.from_json(item) for item in raw_additional))


class ProtocolError(Exception):
    """A rejection, raised so it can travel out of a nested parse.

    The engine turns it into the response body; nothing else catches it. It
    carries the whole :class:`ErrorResult` rather than a code, because the
    secondary violations are as much part of one round trip as the primary is.
    """

    def __init__(self, result: ErrorResult) -> None:
        super().__init__(f"{result.error}: {result.primary.message}")
        self.result = result

    @classmethod
    def single(cls, error: str, message: str, addresses: Iterable[Address] = ()) -> ProtocolError:
        """The common case: one violation and nothing else."""
        return cls(ErrorResult(primary=Violation(error=error, message=message, addresses=tuple(addresses))))


# ---------------------------------------------------------------------------
# Small strict helpers, shared by every shape below
# ---------------------------------------------------------------------------


def _is_declared(value: Any, vocabulary: Collection[str]) -> bool:
    """Whether *value* is a member of a closed STRING vocabulary.

    The ``isinstance`` comes first because ``value in vocabulary`` hashes *value*
    for a ``set`` or ``Mapping``, and an object or array from a hostile peer would
    raise a bare ``TypeError`` out of a parser whose contract is :class:`EnvelopeError`.
    """
    return isinstance(value, str) and value in vocabulary


def _is_array(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _object(data: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise EnvelopeError(
            f"{what} must be an object, not {type(data).__name__}; no request field takes a path, raw YAML or raw text"
        )
    return data


def _closed_keys(data: Mapping[str, Any], allowed: set[str], required: set[str], what: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise EnvelopeError(f"{what} carries no {unknown} (additionalProperties: false)")
    missing = sorted(required - set(data))
    if missing:
        raise EnvelopeError(f"{what} requires {missing}")


def _const(value: Any, expected: str, name: str) -> str:
    if value != expected:
        raise EnvelopeError(f"{name} must be '{expected}', not {value!r}")
    return expected


def _non_empty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise EnvelopeError(f"{name} must be a non-empty string, not {value!r}")
    return value


def _read_form_value(value: Any) -> ReadForm:
    """*value* as a ``$defs/readForm`` member, or the refusal it earns.

    Shared by every shape that carries a read form, so a non-member is refused with
    one sentence; the enum names itself, so no per-site label is needed.
    """
    if value not in READ_FORMS:
        raise EnvelopeError(f"'{value}' is not a read form ({sorted(READ_FORMS)})")
    return value


def _discover_form_value(value: Any) -> DiscoverForm:
    """*value* as a ``$defs/discoverForm`` member, or the refusal it earns."""
    if value not in DISCOVER_FORMS:
        raise EnvelopeError(f"'{value}' is not a discover form ({sorted(DISCOVER_FORMS)})")
    return value


def is_positive_int(value: Any) -> bool:
    """A Python ``int`` of at least one, as a **model value** holds it -- not a protocol field.

    Public for :mod:`~swcraft_tools.editing.link_confirmation`, which composes
    ``R@<version>`` from a ``confirm_links`` update's value: it must be an ``int``,
    never the ``2.0`` that would compose ``R@2.0``. The bound is :func:`integral`'s.
    """
    return isinstance(value, int) and integral(value, 1) is not None


def _address(data: Any, where: str = "") -> Address:
    """``$defs/address``, with the address model's own refusals re-raised as :class:`EnvelopeError`.

    *where* names the position the address was reached at. Without it, two
    operations malformed identically would produce byte-identical violations, and
    :class:`ErrorResult`'s no-repeat rule would turn that into a bare ``ValueError``.
    """
    try:
        return Address.from_json(data)
    except AddressError as exc:
        raise EnvelopeError(f"{where}: {exc}" if where else str(exc)) from exc


def _addresses(data: Mapping[str, Any]) -> tuple[Address, ...]:
    """``addresses`` off a violation, or nothing.

    Takes the whole object because ``data.get`` cannot tell an omitted key from an
    explicit ``null``, which the schema refuses.
    """
    value = data.get("addresses")
    if value is None:
        if "addresses" in data:
            raise EnvelopeError("`addresses` is an array of at least one address, or is omitted entirely")
        return ()
    if not _is_array(value) or not value:
        raise EnvelopeError("`addresses` is an array of at least one address, or is omitted entirely")
    return tuple(_address(item) for item in value)


def _hints(data: Mapping[str, Any]) -> tuple[Hint, ...]:
    """``hints`` off a violation — complete read or discover requests, or nothing.

    An empty array or explicit ``null`` is refused, as for ``addresses``: the
    chain's terminal step omits the key. Each item is parsed by the shape its own
    ``operation`` names, so a hint is a complete request a client can replay verbatim.
    """
    value = data.get("hints")
    if value is None:
        if "hints" in data:
            raise EnvelopeError(_HINTS_SHAPE)
        return ()
    if not _is_array(value) or not value:
        raise EnvelopeError(_HINTS_SHAPE)
    return tuple(_hint(item) for item in value)


#: The one sentence both branches of :func:`_hints` refuse with: an explicit
#: ``null`` and an empty array are the same producer error.
_HINTS_SHAPE = "`hints` is an array of at least one read or discover request, or is omitted entirely"


def _hint(item: Any) -> Hint:
    """One hint: a ``readRequest`` or a ``discoverRequest``, discriminated.

    A change set is refused: every refusal carrying hints is a refusal to read, and
    offering a mutation as the cheaper way to look is what ``$defs/readHints`` refuses too.
    """
    operation = request_operation(item)
    if operation == "discover":
        return DiscoverRequest.from_json(item)
    if operation == "read":
        return ReadRequest.from_json(item)
    raise EnvelopeError(f"a hint is a read or a discover request, not a '{operation}'")


def _measured(data: Mapping[str, Any]) -> Measured | None:
    """``measured`` off a violation, or ``None`` when it was omitted."""
    value = data.get("measured")
    if value is None:
        if "measured" in data:
            raise EnvelopeError("`measured` is a measured block, or is omitted entirely")
        return None
    return Measured.from_json(value)


class _Absent:
    """The absence of a JSON value, which ``None`` cannot express.

    ``$defs/value`` admits ``null``, so an ``after`` of ``null`` (a field cleared)
    differs from no ``after`` at all, and a :class:`WhereClause` ``equals: null``
    differs from an ``exists`` clause. :class:`DeltaEntry`, :class:`WhereClause`,
    :class:`ReadRequest` and :class:`Page` share this one sentinel.
    """

    _instance: ClassVar[_Absent | None] = None

    def __new__(cls) -> _Absent:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "ABSENT"


#: The single :class:`_Absent` instance. Compared with ``is``.
ABSENT = _Absent()


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoverRequest:
    """``$defs/discoverRequest`` — optionally narrowed to one layer, optionally widened to ``full``.

    ``form`` omitted, the server serves :data:`DEFAULT_DISCOVER_FORM` and the
    response states which form it served. An explicit ``null`` is refused: an
    omitted key is how this protocol says nothing.
    """

    layer: str | None = None
    form: DiscoverForm | None = None

    def to_json(self) -> dict[str, Any]:
        request: dict[str, Any] = {"operation": "discover"}
        if self.layer is not None:
            request["layer"] = self.layer
        if self.form is not None:
            request["form"] = self.form
        return request

    @classmethod
    def from_json(cls, data: Any) -> DiscoverRequest:
        _object(data, "a discover request")
        _closed_keys(data, {"operation", "layer", "form"}, {"operation"}, "a discover request")
        _const(data["operation"], "discover", "operation")
        layer = data.get("layer")
        if layer is None and "layer" in data:
            raise EnvelopeError("layer must be a layer name, not null")
        if layer is not None:
            _non_empty_string(layer, "layer")
            if not LAYER_NAME_PATTERN.fullmatch(layer):
                raise EnvelopeError(f"'{layer}' is not a layer name ({LAYER_NAME_PATTERN.pattern})")
        form: DiscoverForm | None = None
        if "form" in data:
            if data["form"] is None:
                # Refused here, in the siblings' "not null" words, because
                # `_discover_form_value` would report the Python repr `'None'`.
                raise EnvelopeError(f"form must be one of {sorted(DISCOVER_FORMS)}, not null")
            form = _discover_form_value(data["form"])
        return cls(layer=layer, form=form)


@dataclass(frozen=True)
class WhereClause:
    """``$defs/whereClause`` — one equality or existence test over a declared field.

    ``equals`` compares the field's published value canonically; ``exists`` asks
    whether the element or member carries a value (a field not carried, ``None``
    and ``""`` are no value, :func:`swcraft_tools.query.holds_no_value`). There are
    no comparison operators and no ``or``.

    ``equals`` defaults to :data:`ABSENT`, not ``None``, because ``$defs/value``
    admits ``null``: ``equals: null`` tests for the cleared value, ``exists: false``
    for the field's absence.

    A ``field`` spelled ``derived.NAME.PATH`` names a computed value of the row
    (``swcraft_tools.query.DERIVED_CLAUSE_PREFIX``). The envelope holds it to
    ``$defs/name`` like every field; whether the name is asked for and the path
    carried is the reader's question (``query.derived_where_refusal``), and the
    clause is applied once the values exist (``query.computed_rows``).
    """

    field: str
    equals: Any = ABSENT
    exists: bool | None = None

    def __post_init__(self) -> None:
        if (self.equals is ABSENT) == (self.exists is None):
            raise EnvelopeError(_WHERE_CLAUSE_SHAPE)

    def to_json(self) -> dict[str, Any]:
        clause: dict[str, Any] = {"field": self.field}
        if self.equals is not ABSENT:
            clause["equals"] = self.equals
        else:
            clause["exists"] = self.exists
        return clause

    @classmethod
    def from_json(cls, data: Any) -> WhereClause:
        _object(data, "a where clause")
        _closed_keys(data, {"field", "equals", "exists"}, {"field"}, "a where clause")
        # `$defs/name` and nothing narrower: which names are declared is the
        # registry's answer, and an undeclared one is `unknown-field`.
        name = _non_empty_string(data["field"], "field")
        if ("equals" in data) == ("exists" in data):
            raise EnvelopeError(_WHERE_CLAUSE_SHAPE)
        if "exists" in data:
            if not isinstance(data["exists"], bool):
                raise EnvelopeError("`exists` is true or false")
            return cls(field=name, exists=data["exists"])
        return cls(field=name, equals=data["equals"])


#: The one sentence :class:`WhereClause`'s constructor and parser both refuse
#: with, so a clause built in Python and one read off the wire get the same words.
_WHERE_CLAUSE_SHAPE = "a where clause carries exactly one of `equals` and `exists`"


def _where(data: Mapping[str, Any]) -> tuple[WhereClause, ...]:
    """``where`` off a read request, or ``()`` when it was omitted entirely.

    An empty array or ``null`` is refused, as for ``hints``: the response echoes
    ``where``, so an admitted empty array would come back as a filter that filtered nothing.
    """
    value = data.get("where")
    if value is None:
        if "where" in data:
            raise EnvelopeError(_WHERE_SHAPE)
        return ()
    if not _is_array(value) or not value:
        raise EnvelopeError(_WHERE_SHAPE)
    return tuple(WhereClause.from_json(clause) for clause in value)


#: The sentence an explicit ``null`` and an empty array are both refused with,
#: for :func:`_hints`' reason: they are one producer error.
_WHERE_SHAPE = "`where` is an array of at least one clause, or is omitted entirely"


def _members(data: Mapping[str, Any]) -> str | None:
    """``members`` off a read request, a response or a joined read, or ``None`` when omitted.

    An explicit ``null`` is refused. The value is held to ``$defs/memberPath`` and
    nothing narrower: which collections a layer declares is the registry's answer,
    so an undeclared step is ``unknown-field``, not a malformed request.
    """
    if "members" not in data:
        return None
    return _checked_members(data["members"])


def _checked_members(value: Any) -> str:
    """*value* as a ``$defs/memberPath``, or the refusal it earns — the parser's and the constructor's one check.

    A ``null``, an empty string and a non-string share :data:`_MEMBERS_SHAPE`; an
    empty step of a path is :data:`_MEMBERS_STEP`.
    """
    if not isinstance(value, str) or not value:
        raise EnvelopeError(_MEMBERS_SHAPE)
    if not MEMBERS_PATH_PATTERN.match(value):
        raise EnvelopeError(_MEMBERS_STEP)
    return value


#: The sentence a ``members`` that is not a field name is refused with, in the
#: parser and in the constructor alike — :data:`_WHERE_CLAUSE_SHAPE`'s rule.
_MEMBERS_SHAPE = "`members` names one declared collection field, or is omitted entirely"

#: The sentence a ``members`` path with an empty step is refused with.
_MEMBERS_STEP = "`members` is a collection field or a dotted path of them, and no step of the path is empty"

#: ``$defs/memberPath``: a name, or names joined by ``.``, none of them empty.
#: Restated in :data:`swcraft_tools.query.MEMBERS_PATH_PATTERN` (the kernel may
#: not import ``editing``); a parity test holds the two equal.
MEMBERS_PATH_PATTERN = re.compile(r"^[^.]+(\.[^.]+)*$")

#: ``$defs/joinAlias``'s pattern: dot-free, so a row path never has two readings.
JOIN_ALIAS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: The keys an identity row carries of its own, which no ``as`` may shadow —
#: ``$defs/joinAlias``'s ``not``/``enum``.
RESERVED_JOIN_ALIASES: frozenset[str] = frozenset({"element", "through", "member", "key", "derived"})


def _derived_names(value: Any) -> tuple[str, ...]:
    """``$defs/derivedNames``: at least one name, none twice, each a non-empty string.

    Which names exist is the vocabulary's answer (``unknown-field`` from the
    engine), so a name is held to ``$defs/name`` and nothing narrower.
    """
    if not _is_array(value) or not value:
        raise EnvelopeError(_DERIVED_SHAPE)
    names = tuple(value)
    if not all(isinstance(name, str) and name for name in names):
        raise EnvelopeError(_DERIVED_SHAPE)
    if len(set(names)) != len(names):
        raise EnvelopeError("`derived` names each computed value once (uniqueItems)")
    return names


_DERIVED_SHAPE = "`derived` is an array of at least one computed-value name, or is omitted entirely"


def _derived(data: Mapping[str, Any]) -> tuple[str, ...]:
    """``derived`` off a read request, a response or a joined read, or ``()`` when omitted."""
    if "derived" not in data:
        return ()
    return _derived_names(data["derived"])


def _layer_only(address: Address, what: str) -> Address:
    """*address* when it names a whole layer; the refusal ``$defs/layerOnlyAddress`` states otherwise."""
    if address.element is not None:
        raise EnvelopeError(
            f"{what} is answered over a layer's rows, so its address names a layer and no element "
            "(an element, field, member or entry address is not a set of rows)"
        )
    return address


@dataclass(frozen=True)
class JoinCondition:
    """``$defs/joinCondition`` — the value at ``left`` in the row so far equals the one at ``right`` in the joined row.

    Both are row paths, and the envelope holds each to ``$defs/name`` alone: the
    path grammar is a function of the declared row **shape**, which only the
    engine knows, so a path outside it is the engine's ``schema-violation``
    rather than a malformed request.
    """

    left: str
    right: str

    def __post_init__(self) -> None:
        for side in (self.left, self.right):
            if not isinstance(side, str) or not side:
                raise EnvelopeError(_JOIN_CONDITION_SHAPE)

    def to_json(self) -> dict[str, Any]:
        return {"left": self.left, "right": self.right}

    @classmethod
    def from_json(cls, data: Any) -> JoinCondition:
        _object(data, "a join condition")
        _closed_keys(data, {"left", "right"}, {"left", "right"}, "a join condition")
        return cls(left=data["left"], right=data["right"])


_JOIN_CONDITION_SHAPE = "a join condition's `left` and `right` are each a non-empty row path"
_JOIN_ON_SHAPE = "a join entry's `on` is an array of at least one condition"


@dataclass(frozen=True)
class JoinedRead:
    """``$defs/joinedRead`` — the read a join entry combines the outer rows with.

    A layer address with optionally ``members``, ``where`` and ``derived``, and
    nothing else: no ``form`` (its rows are always wrapped), no page (``limit``,
    ``after``) and no ``join`` of its own. A forbidden key is refused by the
    closed key set, so a Python caller cannot express one at all.
    """

    address: Address
    members: str | None = None
    where: tuple[WhereClause, ...] = ()
    derived: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "where", tuple(self.where))
        object.__setattr__(self, "derived", tuple(self.derived))
        _layer_only(self.address, "a joined read")
        if self.members is not None:
            _checked_members(self.members)
        if self.derived:
            _derived_names(self.derived)

    def to_json(self) -> dict[str, Any]:
        joined: dict[str, Any] = {"address": self.address.to_json()}
        if self.members is not None:
            joined["members"] = self.members
        if self.where:
            joined["where"] = [clause.to_json() for clause in self.where]
        if self.derived:
            joined["derived"] = list(self.derived)
        return joined

    @classmethod
    def from_json(cls, data: Any) -> JoinedRead:
        _object(data, "a joined read")
        _closed_keys(data, {"address", "members", "where", "derived"}, {"address"}, "a joined read")
        return cls(
            address=_address(data["address"]),
            members=_members(data),
            where=_where(data),
            derived=_derived(data),
        )


@dataclass(frozen=True)
class JoinEntry:
    """One item of ``$defs/join``: the joined read, its equalities, and the key its match nests under.

    ``with`` and ``as`` are Python keywords, so the attributes are :attr:`read` and
    :attr:`alias`. That the alias repeats no earlier entry's is :func:`check_rows`'
    check, because only the whole array can state it — and the schema cannot, the
    one declared one-sided exemption of the parity gate.
    """

    read: JoinedRead
    on: tuple[JoinCondition, ...]
    alias: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "on", tuple(self.on))
        if not self.on:
            raise EnvelopeError(_JOIN_ON_SHAPE)
        if not isinstance(self.alias, str) or not JOIN_ALIAS_PATTERN.fullmatch(self.alias):
            raise EnvelopeError(
                f"a join alias matches {JOIN_ALIAS_PATTERN.pattern}: it is dot-free, so a row path has one reading"
            )
        if self.alias in RESERVED_JOIN_ALIASES:
            raise EnvelopeError(
                f"a join alias may not be '{self.alias}', a key every row carries of its own "
                f"({sorted(RESERVED_JOIN_ALIASES)})"
            )

    def to_json(self) -> dict[str, Any]:
        return {"with": self.read.to_json(), "on": [condition.to_json() for condition in self.on], "as": self.alias}

    @classmethod
    def from_json(cls, data: Any) -> JoinEntry:
        _object(data, "a join entry")
        _closed_keys(data, {"with", "on", "as"}, {"with", "on", "as"}, "a join entry")
        conditions = data["on"]
        if not _is_array(conditions) or not conditions:
            raise EnvelopeError(_JOIN_ON_SHAPE)
        return cls(
            read=JoinedRead.from_json(data["with"]),
            on=tuple(JoinCondition.from_json(condition) for condition in conditions),
            alias=data["as"],
        )


def _join(data: Mapping[str, Any]) -> tuple[JoinEntry, ...]:
    """``join`` off a read request or response, or ``()`` when omitted."""
    if "join" not in data:
        return ()
    value = data["join"]
    if not _is_array(value) or not value:
        raise EnvelopeError("`join` is an array of at least one entry, or is omitted entirely")
    # Counted before any entry is parsed, as the CLI's parser does, so an
    # over-cap request earns the cap sentence on both surfaces.
    if len(value) > _kernel_query.request.JOIN_MAX_ENTRIES:
        raise EnvelopeError(_kernel_query.request.join_entries_refusal(len(value)))
    return tuple(JoinEntry.from_json(entry) for entry in value)


def check_rows(address: Address, form: Any, derived: tuple[str, ...], join: tuple[JoinEntry, ...]) -> None:
    """The envelope rules of a read that answers identity rows — ``$defs/rowForm`` and the alias rule.

    One function for the request and its echo, so a response built in Python cannot
    state a combination the request could not. A read carrying ``derived`` or
    ``join`` states ``form: "index"`` explicitly (``index`` is never a default) and
    is addressed at a layer; a ``join`` carries at most ``query.JOIN_MAX_ENTRIES``
    entries, refused with the CLI parser's sentence; no join entry repeats an
    earlier entry's ``as``.
    """
    if not derived and not join:
        return
    if len(join) > _kernel_query.request.JOIN_MAX_ENTRIES:
        raise EnvelopeError(_kernel_query.request.join_entries_refusal(len(join)))
    keys = " and ".join(name for name, present in (("`derived`", derived), ("`join`", join)) if present)
    if form != "index":
        raise EnvelopeError(f'a read carrying {keys} answers identity rows, so it states `form: "index"`')
    _layer_only(address, f"a read carrying {keys}")
    seen: set[str] = set()
    for entry in join:
        if entry.alias in seen:
            raise EnvelopeError(f"join alias '{entry.alias}' is already an earlier entry's `as`")
        seen.add(entry.alias)


@dataclass(frozen=True)
class ReadRequest:
    """``$defs/readRequest`` — unscoped, unaudited, and carrying no baseline.

    ``form`` selects how *wide* the answer is, never how deep; omitted, the server
    applies its default and echoes the form it answered in. ``where`` is
    AND-composed clauses at a layer or collection address. ``limit`` and ``after``
    are a **keyset** page, after the filter and before the form; ``after`` names
    the identity to continue past and requires ``limit``. ``members`` names a
    declared collection field, or a dotted path of them, at a layer address alone:
    every member across the layer beside its owner's identity. ``derived`` and
    ``join`` ask for identity rows.
    """

    address: Address
    form: ReadForm | None = None
    where: tuple[WhereClause, ...] = ()
    limit: int | None = None
    after: Any = ABSENT
    members: str | None = None
    derived: tuple[str, ...] = ()
    join: tuple[JoinEntry, ...] = ()

    def __post_init__(self) -> None:
        # A list from a Python caller becomes a tuple, so the request equals
        # the same one read off the wire.
        object.__setattr__(self, "where", tuple(self.where))
        object.__setattr__(self, "derived", tuple(self.derived))
        object.__setattr__(self, "join", tuple(self.join))
        if self.after is not ABSENT and self.limit is None:
            raise EnvelopeError(_AFTER_NEEDS_LIMIT)
        if self.limit is not None:
            limit = integral(self.limit, 1)
            if limit is None:
                raise EnvelopeError(_LIMIT_SHAPE)
            object.__setattr__(self, "limit", limit)
        if self.members is not None:
            _checked_members(self.members)
        if self.derived:
            _derived_names(self.derived)
        check_rows(self.address, self.form, self.derived, self.join)

    def to_json(self) -> dict[str, Any]:
        request: dict[str, Any] = {"operation": "read", "address": self.address.to_json()}
        if self.members is not None:
            request["members"] = self.members
        if self.form is not None:
            request["form"] = self.form
        if self.where:
            request["where"] = [clause.to_json() for clause in self.where]
        if self.limit is not None:
            request["limit"] = self.limit
        if self.after is not ABSENT:
            request["after"] = self.after
        if self.derived:
            request["derived"] = list(self.derived)
        if self.join:
            request["join"] = [entry.to_json() for entry in self.join]
        return request

    @classmethod
    def from_json(cls, data: Any) -> ReadRequest:
        _object(data, "a read request")
        _closed_keys(
            data,
            {"operation", "address", "form", "where", "limit", "after", "members", "derived", "join"},
            {"operation", "address"},
            "a read request",
        )
        _const(data["operation"], "read", "operation")
        limit = None
        if "limit" in data:
            # Validated on presence: an explicit `limit: null` is refused, not
            # read as "no page" (`type: integer` does not admit `null`).
            limit = integral(data["limit"], 1)
            if limit is None:
                raise EnvelopeError(_LIMIT_SHAPE)
        if "after" in data and limit is None:
            raise EnvelopeError(_AFTER_NEEDS_LIMIT)
        return cls(
            address=_address(data["address"]),
            form=_read_form(data),
            where=_where(data),
            limit=limit,
            # A `whole_value` collection's identity is its member, which may be
            # `null`, so an explicit `after: null` is a cursor told from an
            # omitted key by `ABSENT`.
            after=data["after"] if "after" in data else ABSENT,
            members=_members(data),
            derived=_derived(data),
            join=_join(data),
        )


#: The two sentences the page's request keys are refused with, in the
#: constructor and the parser alike.
_LIMIT_SHAPE = "`limit` is a page size of at least one, or is omitted entirely"
_AFTER_NEEDS_LIMIT = "`after` continues a page, so it is sent with a `limit`; a page is a bounded window"


def _read_form(data: Mapping[str, Any]) -> ReadForm | None:
    """``form`` off a read request, or ``None`` when it was omitted; an explicit ``null`` is refused."""
    form = data.get("form")
    if form is None:
        if "form" in data:
            raise EnvelopeError(f"form must be one of {sorted(READ_FORMS)}, not null")
        return None
    return _read_form_value(form)


@dataclass(frozen=True)
class CreateOperation:
    """``$defs/createOperation`` — an element address and the complete payload."""

    address: Address
    value: Any

    operation: str = field(default="create", init=False)

    def to_json(self) -> dict[str, Any]:
        return {"operation": "create", "address": self.address.to_json(), "value": self.value}


@dataclass(frozen=True)
class UpdateOperation:
    """``$defs/updateOperation`` — a *field* address, so ``path`` is required."""

    address: Address
    value: Any
    #: Confirm every link to the requirement at the new version as well:
    #: on the update of a requirement's ``version`` only, the
    #: server adds, for each ``satisfies`` member naming it at another version
    #: or at none, a ``remove`` of that member and a ``create`` of
    #: ``<id>@<new>`` (only the ``remove`` when the list holds that already), and
    #: the human reviews each as a delta row marked ``origin: "server"``.
    #: ``False`` is omitted on the wire.
    confirm_links: bool = False

    operation: str = field(default="update", init=False)

    def to_json(self) -> dict[str, Any]:
        operation: dict[str, Any] = {"operation": "update", "address": self.address.to_json(), "value": self.value}
        if self.confirm_links:
            operation["confirm_links"] = True
        return operation


@dataclass(frozen=True)
class RemoveOperation:
    """``$defs/removeOperation`` — carries no value, deliberately."""

    address: Address
    #: Remove what refers to the removed component as well:
    #: the server adds one ordinary operation per referrer — a connector, a
    #: composition member and its delegations, a task's runnable entry, a
    #: sequence step (the sequence itself when it was its last step) — and the
    #: human reviews each as a delta row marked ``origin: "server"``. Without it
    #: a remove that leaves a referrer is refused as before. ``False`` is
    #: omitted on the wire.
    with_dependents: bool = False

    operation: str = field(default="remove", init=False)

    def to_json(self) -> dict[str, Any]:
        operation: dict[str, Any] = {"operation": "remove", "address": self.address.to_json()}
        if self.with_dependents:
            operation["with_dependents"] = True
        return operation


ChangeOperation = CreateOperation | UpdateOperation | RemoveOperation

#: The sentences a refused operation gains at :func:`parse_change_operation`'s
#: own call sites: what to send instead, beside what was wrong.
REMOVE_CARRIES_NO_VALUE = "a remove carries no value: to replace a list member, remove it and create the new one"
CREATE_CARRIES_A_VALUE = "a create carries the new element or member in `value`"
UPDATE_CARRIES_A_VALUE = (
    "an update carries the field's whole new value in `value`; to confirm every link to a requirement at a new "
    "version, update its `version` with `confirm_links: true`"
)


def parse_change_operation(data: Any, position: int) -> ChangeOperation:
    """``$defs/changeOperation`` at *position* of the operations array.

    Raises:
        EnvelopeError: naming the position, so a client reading a message about
            a five-operation change set knows which one it is about.
    """
    where = f"operation {position}"
    _object(data, where)
    if "operation" not in data:
        raise EnvelopeError(f"{where} requires ['operation']")
    name = data["operation"]
    if name not in CHANGE_OPERATION_NAMES:
        raise EnvelopeError(
            f"{where} names '{name}', which is not one of {list(CHANGE_OPERATION_NAMES)}. "
            "A rename is a remove, a create and the reference updates in the same change set"
        )

    if name == "remove":
        try:
            _closed_keys(data, {"operation", "address", "with_dependents"}, {"operation", "address"}, where)
        except EnvelopeError as refused:
            if "value" not in data:
                raise
            raise EnvelopeError(f"{refused}; {REMOVE_CARRIES_NO_VALUE}") from None
        if "with_dependents" in data and not isinstance(data["with_dependents"], bool):
            raise EnvelopeError(f"{where}: with_dependents must be a boolean")
        return RemoveOperation(
            address=_element_address(data["address"], where), with_dependents=data.get("with_dependents") is True
        )

    allowed = {"operation", "address", "value"} | ({"confirm_links"} if name == "update" else set())
    try:
        _closed_keys(data, allowed, {"operation", "address", "value"}, where)
    except EnvelopeError as refused:
        if "value" in data or set(data) - allowed:
            raise
        sentence = CREATE_CARRIES_A_VALUE if name == "create" else UPDATE_CARRIES_A_VALUE
        raise EnvelopeError(f"{refused}; {sentence}") from None
    if name == "create":
        return CreateOperation(address=_element_address(data["address"], where), value=data["value"])
    if "confirm_links" in data and not isinstance(data["confirm_links"], bool):
        raise EnvelopeError(f"{where}: confirm_links must be a boolean")
    return UpdateOperation(
        address=_field_address(data["address"], where),
        value=data["value"],
        confirm_links=data.get("confirm_links") is True,
    )


def _element_address(data: Any, where: str) -> Address:
    """``$defs/elementAddress``: an address that names an element."""
    address = _address(data, where)
    if not address.is_element_address:
        raise EnvelopeError(f"{where} needs an address naming an element (elementAddress requires 'element')")
    return address


def _field_address(data: Any, where: str) -> Address:
    """``$defs/fieldAddress``: an element plus a path into it."""
    address = _address(data, where)
    if not address.is_field_address:
        raise EnvelopeError(
            f"{where} needs an address naming a field, member or map entry (fieldAddress requires 'element' and 'path')"
        )
    return address


@dataclass(frozen=True)
class ChangeSetRequest:
    """``$defs/changeSetRequest`` — ``{baseline, operations}``, all-or-nothing.

    ``baseline`` is required by the shape, so there is no "just apply it" path:
    concurrency control is a property of the request. An empty change set is not a
    request either.
    """

    baseline: str
    operations: tuple[ChangeOperation, ...]
    intent: str | None = None
    #: Ask the server to add the operation a deterministic rule's unique repair
    #: needs when the staged result breaks it. The
    #: added operations are marked in the delta (``DeltaEntry.origin``), so the
    #: human still reviews every row; a repair that is not unique is refused as
    #: before. ``False`` is omitted on the wire.
    complete: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "operations", tuple(self.operations))

    def to_json(self) -> dict[str, Any]:
        request: dict[str, Any] = {"operation": "change_set", "baseline": self.baseline}
        if self.intent is not None:
            request["intent"] = self.intent
        if self.complete:
            request["complete"] = True
        request["operations"] = [operation.to_json() for operation in self.operations]
        return request

    @classmethod
    def from_json(cls, data: Any) -> ChangeSetRequest:
        baseline, intent, raw_operations, complete = parse_change_set_envelope(data)
        return cls(
            baseline=baseline,
            intent=intent,
            operations=tuple(parse_change_operation(item, position) for position, item in enumerate(raw_operations)),
            complete=complete,
        )


def parse_change_set_envelope(data: Any) -> tuple[str, str | None, list[Any], bool]:
    """The change-set envelope *around* its operations, checked on its own.

    Split out so pass A confirms ``operations`` is a non-empty array before walking
    it; only then may more than one violation be reported.

    Returns:
        ``(baseline, intent, raw_operations, complete)``, the operations unparsed
        and ``complete`` ``False`` when the key is absent.
    """
    _object(data, "a change_set request")
    _closed_keys(
        data,
        {"operation", "baseline", "intent", "operations", "complete"},
        {"operation", "baseline", "operations"},
        "a change_set request",
    )
    if "complete" in data and not isinstance(data["complete"], bool):
        raise EnvelopeError(f"complete must be a boolean, not {type(data['complete']).__name__}")
    _const(data["operation"], "change_set", "operation")
    baseline = _non_empty_string(data["baseline"], "baseline")
    if not REVISION_TOKEN_PATTERN.fullmatch(baseline):
        raise EnvelopeError(f"'{baseline}' is not a revisionToken ({REVISION_TOKEN_PATTERN.pattern})")
    intent = data.get("intent")
    if intent is not None or "intent" in data:
        intent = _non_empty_string(intent, "intent")
    raw_operations = data["operations"]
    if not _is_array(raw_operations):
        raise EnvelopeError(f"operations must be an array, not {type(raw_operations).__name__}")
    if not raw_operations:
        raise EnvelopeError("an empty change set is not a request (minItems: 1)")
    return baseline, intent, list(raw_operations), data.get("complete", False)


#: The most addresses one batched read may carry (``$defs/readManyRequest``'s ``maxItems``).
READ_BATCH_MAX = 16

#: The most elements a layer may hold for the model map to list its names
#: (``$defs/modelMapResponse``'s ``maxItems``); a larger layer is counted only.
#: It bounds names, not bytes, so the shrink loop in
#: :func:`~swcraft_tools.editing.read.model_map` holds the budget.
MODEL_MAP_LIST_MAX = 50


@dataclass(frozen=True)
class ReadManyRequest:
    """``read`` with ``addresses``: several reads answered in one response, in order.

    Each address is answered as a single ``read`` of it would be — its own
    default form unless ``form`` names one for all, and a refusal of one
    address is that entry's ``errorResult`` rather than a refusal of the batch.
    The budget is measured over the whole answer. No ``where``, page, member
    slice, ``derived`` or ``join``: those belong to one address, and a batch of
    sets is a batch of single reads the client can state one by one.
    """

    addresses: tuple[Address, ...]
    form: ReadForm | None = None

    def to_json(self) -> dict[str, Any]:
        request: dict[str, Any] = {"operation": "read", "addresses": [address.to_json() for address in self.addresses]}
        if self.form is not None:
            request["form"] = self.form
        return request

    @classmethod
    def from_json(cls, data: Any) -> ReadManyRequest:
        _object(data, "a batched read request")
        _closed_keys(data, {"operation", "addresses", "form"}, {"operation", "addresses"}, "a batched read request")
        _const(data["operation"], "read", "operation")
        raw = data["addresses"]
        if not _is_array(raw) or not raw or len(raw) > READ_BATCH_MAX:
            raise EnvelopeError(f"addresses must be an array of 1 to {READ_BATCH_MAX} addresses")
        return cls(
            addresses=tuple(_address(item, f"addresses[{position}]") for position, item in enumerate(raw)),
            form=_read_form(data),
        )


@dataclass(frozen=True)
class ModelMapRequest:
    """``read`` with no address: the bounded model map and the baseline token.

    Every layer's element count, the identities of the layers holding at most
    :data:`MODEL_MAP_LIST_MAX` elements, and the baseline token — bounded by the
    number of layers, never by the model.
    """

    def to_json(self) -> dict[str, Any]:
        return {"operation": "read"}

    @classmethod
    def from_json(cls, data: Any) -> ModelMapRequest:
        _object(data, "a model map request")
        _closed_keys(data, {"operation"}, {"operation"}, "a model map request (a read with no address)")
        _const(data["operation"], "read", "operation")
        return cls()


#: The most names one ``find`` request may carry (``addresses``' bound).
FIND_MAX = READ_BATCH_MAX

#: The most candidates a ``find`` answers per name (:mod:`~swcraft_tools.editing.resolve` imports it):
#: the ceiling the schema's ``maxItems`` and :func:`parse_response` check. A request of
#: more than three names answers fewer each (:func:`candidates_per_name`).
CANDIDATES_PER_NAME = 5

#: The most candidates one ``find`` by ``names`` answers in all: the names' own
#: bound, so the answer is bounded by the request rather than by the model. The
#: server's behaviour only: neither the schema nor :func:`parse_response` checks it.
FIND_CANDIDATES_TOTAL = FIND_MAX


def candidates_per_name(names: int) -> int:
    """The candidates each name of a ``find`` of *names* names answers.

    ``min(CANDIDATES_PER_NAME, max(1, FIND_CANDIDATES_TOTAL // names))``: one to
    three names keep five each, nine to sixteen one — never more than
    :data:`FIND_CANDIDATES_TOTAL` in all. Zero names reads as one.
    """
    return min(CANDIDATES_PER_NAME, max(1, FIND_CANDIDATES_TOTAL // max(1, names)))


#: A ``find`` candidate's tiers, best first (:mod:`~swcraft_tools.editing.resolve` ranks by this order).
CANDIDATE_TIERS = ("exact", "case", "word", "near")

#: The forms of a ``find`` request, one key each and mutually exclusive: the
#: names resolved across the model, the words searched in the requirements'
#: text, or the trace search's candidates for one requirement. A request
#: carries exactly one (:func:`parse_find_request`).
FIND_FORMS = ("names", "text", "for_requirement")

#: The most requirements a ``find`` by ``text`` answers; ``more`` counts the rest.
#: The read batch's cap, as :data:`FIND_MAX` is: the candidates are addresses
#: one ``read_many`` takes back in a single call.
FIND_TEXT_MAX = READ_BATCH_MAX

#: The places a ``find`` for a requirement answers when the request names none.
#: ``experiment trace`` defaults to ``search.candidates`` = 5; the two are not one value.
FIND_CANDIDATES_DEFAULT = 3

#: The most places a ``find`` for a requirement may ask for.
FIND_CANDIDATES_MAX = 10

#: The most existing links a ``find`` for a requirement lists; ``existing_links_more``
#: counts the rest and ``existing_links_read`` pages them all. Without it a widely
#: linked requirement would be refused ``over-budget`` with no narrower ``find``.
FIND_EXISTING_LINKS_MAX = READ_BATCH_MAX

#: The trace search's band of a row, strongest first: ``trace_search.BANDS``, held equal to it
#: and to the schema's enum by ``test_editing_protocol_models.py``.
FIND_CANDIDATE_BANDS = ("strong", "medium", "weak")


@dataclass(frozen=True)
class FindRequest:
    """``$defs/findRequest``: each name resolved across every layer and the member collections.

    The answer is bounded by the number of names, never by the model: up to five
    candidates per name, each with its address and a one-line identity, or
    ``"none"`` (:mod:`swcraft_tools.editing.resolve`).
    """

    names: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {"operation": "find", "names": list(self.names)}

    @classmethod
    def from_json(cls, data: Any) -> FindRequest:
        _object(data, "a find request")
        _closed_keys(data, {"operation", "names"}, {"operation", "names"}, "a find request")
        _const(data["operation"], "find", "operation")
        raw = data["names"]
        if (
            not _is_array(raw)
            or not raw
            or len(raw) > FIND_MAX
            or not all(isinstance(name, str) and name.strip() for name in raw)
        ):
            raise EnvelopeError(f"names must be an array of 1 to {FIND_MAX} non-empty strings")
        return cls(names=tuple(raw))


@dataclass(frozen=True)
class FindTextRequest:
    """``$defs/findRequest`` with ``text``: the requirements whose text holds every word of it.

    Each word of *text* must equal or start a distinct word of a requirement's
    ``id``, ``title`` and ``description`` (:mod:`swcraft_tools.editing.resolve`),
    never a substring. At most :data:`FIND_TEXT_MAX` answers in ``id`` order,
    ``more`` counting the rest.
    """

    text: str

    def to_json(self) -> dict[str, Any]:
        return {"operation": "find", "text": self.text}

    @classmethod
    def from_json(cls, data: Any) -> FindTextRequest:
        _object(data, "a find request")
        _closed_keys(data, {"operation", "text"}, {"operation", "text"}, "a find request")
        _const(data["operation"], "find", "operation")
        if not isinstance(data["text"], str) or not data["text"].strip():
            raise EnvelopeError("text must be a non-empty string")
        return cls(text=data["text"])


@dataclass(frozen=True)
class FindForRequirementRequest:
    """``$defs/findRequest`` with ``for_requirement``: the trace search's candidates for one requirement.

    The ranking ``swcraft experiment trace`` computes under its default
    configuration, projected one to one and never re-ordered
    (:mod:`swcraft_tools.editing.trace_candidates`). *candidates* is the number of
    places, 1 to :data:`FIND_CANDIDATES_MAX`, default :data:`FIND_CANDIDATES_DEFAULT`.
    """

    for_requirement: str
    candidates: int = FIND_CANDIDATES_DEFAULT

    def to_json(self) -> dict[str, Any]:
        return {"operation": "find", "for_requirement": self.for_requirement, "candidates": self.candidates}

    @classmethod
    def from_json(cls, data: Any) -> FindForRequirementRequest:
        _object(data, "a find request")
        _closed_keys(
            data, {"operation", "for_requirement", "candidates"}, {"operation", "for_requirement"}, "a find request"
        )
        _const(data["operation"], "find", "operation")
        identifier = data["for_requirement"]
        if not isinstance(identifier, str) or not identifier.strip():
            raise EnvelopeError("for_requirement must be a non-empty requirement id")
        candidates = integral(data.get("candidates", FIND_CANDIDATES_DEFAULT), 1)
        if candidates is None or candidates > FIND_CANDIDATES_MAX:
            raise EnvelopeError(f"candidates must be an integer from 1 to {FIND_CANDIDATES_MAX}")
        return cls(for_requirement=identifier, candidates=candidates)


def parse_find_request(data: Any) -> FindRequest | FindTextRequest | FindForRequirementRequest:
    """A ``find`` request in its form, exactly one of :data:`FIND_FORMS`; ``candidates`` with ``for_requirement``."""
    _object(data, "a find request")
    _closed_keys(data, {"operation", *FIND_FORMS, "candidates"}, {"operation"}, "a find request")
    forms = [form for form in FIND_FORMS if form in data]
    if len(forms) != 1:
        raise EnvelopeError(f"a find request carries exactly one of {list(FIND_FORMS)}, not {forms}")
    if forms == ["for_requirement"]:
        return FindForRequirementRequest.from_json(data)
    if "candidates" in data:
        raise EnvelopeError("candidates belongs to a find for_requirement alone: the number of places it ranks")
    if forms == ["text"]:
        return FindTextRequest.from_json(data)
    return FindRequest.from_json(data)


@dataclass(frozen=True)
class CountRequest:
    """``read`` with ``form: "count"``: the number of things the set read would answer, and nothing else.

    The set is the one a ``readRequest`` with the same ``address``, ``where``,
    ``members``, ``derived`` and ``join`` selects — rows of an identity-row read,
    members of a member slice, elements of a layer, members of a collection —
    counted after the filter and the joins. No page: a count is one number.
    """

    read: ReadRequest

    def to_json(self) -> dict[str, Any]:
        request = self.read.to_json()
        request["form"] = "count"
        return request

    @classmethod
    def from_json(cls, data: Any) -> CountRequest:
        _object(data, "a count read request")
        if data.get("form") != "count":
            # `$defs/countRequest` requires `form: "count"`: the parser checks it rather than
            # writing it, so a direct call cannot turn another form, or none, into a count.
            raise EnvelopeError('a count read request states `form: "count"`')
        if "addresses" in data:
            raise EnvelopeError(
                "a count counts the set one `address` names: `addresses` batches reads and is refused with it"
            )
        if "limit" in data or "after" in data:
            raise EnvelopeError("a count is one number: `limit` and `after` page a set and are refused with it")
        return cls(read=ReadRequest.from_json({**data, "form": "index"}))


def parse_read_request(data: Any) -> ReadRequest | ReadManyRequest | ModelMapRequest | CountRequest:
    """A ``read`` request in its shape: one address, several, a count, or none (the map)."""
    if isinstance(data, Mapping) and data.get("form") == "count":
        return CountRequest.from_json(data)
    if isinstance(data, Mapping) and "addresses" in data:
        return ReadManyRequest.from_json(data)
    if isinstance(data, Mapping) and "address" not in data:
        return ModelMapRequest.from_json(data)
    return ReadRequest.from_json(data)


Request = (
    DiscoverRequest
    | ReadRequest
    | ReadManyRequest
    | ModelMapRequest
    | FindRequest
    | FindTextRequest
    | FindForRequirementRequest
    | CountRequest
    | ChangeSetRequest
)

#: What one item of ``$defs/readHints`` may be: a complete request of one of the
#: two read-only shapes, replayable as it stands (:func:`_hint`).
Hint = DiscoverRequest | ReadRequest


def request_operation(data: Any) -> str:
    """The request discriminator, or an :class:`EnvelopeError`.

    Closed to :data:`REQUEST_OPERATIONS`; ``rename`` has no branch, so it cannot be
    added back without changing the schema.
    """
    _object(data, "a request")
    if "operation" not in data:
        raise EnvelopeError(f"a request requires ['operation'], one of {list(REQUEST_OPERATIONS)}")
    name = data["operation"]
    if name not in REQUEST_OPERATIONS:
        raise EnvelopeError(
            f"'{name}' is not a protocol request; the discriminator is closed to {list(REQUEST_OPERATIONS)}"
        )
    return str(name)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Passport:
    """``$defs/projectPassport``: three fields, and **no storage root**.

    A dataclass because it is the producer as well as the parser
    (:mod:`swcraft_tools.editing.discovery` constructs one): ``model_root``,
    ``dist_root``, ``views_root`` and ``model_hash`` have no field to arrive through.
    """

    name: str
    arxml_version: str
    swcraft_version: str

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arxml_version": self.arxml_version,
            "swcraft_version": self.swcraft_version,
        }

    @classmethod
    def from_json(cls, data: Any) -> Passport:
        _object(data, "a projectPassport")
        _closed_keys(
            data,
            {"name", "arxml_version", "swcraft_version"},
            {"name", "arxml_version", "swcraft_version"},
            "a projectPassport",
        )
        if not _is_declared(data["arxml_version"], _ARXML_VERSIONS):
            raise EnvelopeError(f"arxml_version must be one of {sorted(_ARXML_VERSIONS)}")
        return cls(
            name=_non_empty_string(data["name"], "project name"),
            arxml_version=str(data["arxml_version"]),
            swcraft_version=_non_empty_string(data["swcraft_version"], "swcraft_version"),
        )


@dataclass(frozen=True)
class DiscoverCapability:
    """``$defs/discoverCapability`` — how ``discover`` answers.

    Nested in :class:`ReadsCapability` because the budget is one bound over both
    operations. ``discover`` is addressed by nothing, so it has one flat default
    rather than a table.
    """

    forms: tuple[str, ...]
    default_form: str

    def to_json(self) -> dict[str, Any]:
        return {"forms": list(self.forms), "default_form": self.default_form}

    @classmethod
    def from_json(cls, data: Any) -> DiscoverCapability:
        _object(data, "a discoverCapability")
        keys = {"forms", "default_form"}
        _closed_keys(data, keys, keys, "a discoverCapability")
        forms = data["forms"]
        if not _is_array(forms) or not forms or any(form not in DISCOVER_FORMS for form in forms):
            raise EnvelopeError(f"forms must be a non-empty array of discover forms ({sorted(DISCOVER_FORMS)})")
        return cls(
            forms=tuple(str(form) for form in forms),
            default_form=_discover_form_value(data["default_form"]),
        )


@dataclass(frozen=True)
class JoinCapability:
    """``$defs/readsCapability``'s ``join`` block — how large a ``join`` may grow.

    ``row_ceiling`` is the most rows a join may build in total, summed over every
    entry and counted exactly before each entry builds; above it the request is
    refused ``over-budget`` with no hints, under it the join is built and paged like
    any set. It bounds the combined rows, not the joined reads, whose slices and
    ``derived`` values are computed in full on every page; ``max_entries`` (refused
    ``schema-violation`` at parse) bounds that repetition. Both are read off the
    engine's own bounds at the call by :func:`swcraft_tools.editing.read.reads_capability`.
    """

    row_ceiling: int
    max_entries: int

    def to_json(self) -> dict[str, Any]:
        return {"row_ceiling": self.row_ceiling, "max_entries": self.max_entries}

    @classmethod
    def from_json(cls, data: Any) -> JoinCapability:
        _object(data, "a joinCapability")
        keys = {"row_ceiling", "max_entries"}
        _closed_keys(data, keys, keys, "a joinCapability")
        row_ceiling = integral(data["row_ceiling"], 1)
        if row_ceiling is None:
            raise EnvelopeError("row_ceiling must be a positive integer")
        max_entries = integral(data["max_entries"], 1)
        if max_entries is None:
            raise EnvelopeError("max_entries must be a positive integer")
        return cls(row_ceiling=row_ceiling, max_entries=max_entries)


@dataclass(frozen=True)
class ReadsCapability:
    """``$defs/readsCapability`` — how ``read`` answers, and what it refuses above.

    Published by ``discover`` as one top-level key, never per layer and never in the
    passport: the forms and the budget are facts about this server. It carries
    :class:`DiscoverCapability` too, because ``discover`` is measured against the
    same budget. The values are handed in by
    :func:`swcraft_tools.editing.read.reads_capability`, read off the engine's own
    constants at the call, so what is advertised and what is enforced cannot drift apart.
    """

    forms: tuple[str, ...]
    default_form: Mapping[AddressShape, ReadForm]
    budget_wire_bytes: int
    hint_page_size: int
    discover: DiscoverCapability
    #: The computed-value vocabulary a read's ``derived`` names, each entry as
    #: ``discover`` publishes it:
    #: ``{"name", "at", "rows", "shape"}``. Handed in by
    #: :func:`swcraft_tools.editing.read.reads_capability`, which reads it off
    #: ``validator.DERIVED`` at the call — the vocabulary the engine answers.
    derived: tuple[Mapping[str, Any], ...]
    #: The join row ceiling and entry cap (:class:`JoinCapability`).
    join: JoinCapability

    def to_json(self) -> dict[str, Any]:
        return {
            "forms": list(self.forms),
            "default_form": dict(self.default_form),
            "budget_wire_bytes": self.budget_wire_bytes,
            "hint_page_size": self.hint_page_size,
            "discover": self.discover.to_json(),
            "derived": [_copied_json(entry) for entry in self.derived],
            "join": self.join.to_json(),
        }

    @classmethod
    def from_json(cls, data: Any) -> ReadsCapability:
        _object(data, "a readsCapability")
        keys = {"forms", "default_form", "budget_wire_bytes", "hint_page_size", "discover", "derived", "join"}
        _closed_keys(data, keys, keys, "a readsCapability")
        # `not data["forms"]` too: `any` over an empty list is `False`, and the
        # schema says `minItems: 1`.
        if not _is_array(data["forms"]) or not data["forms"] or any(form not in READ_FORMS for form in data["forms"]):
            raise EnvelopeError(f"forms must be a non-empty array of read forms ({sorted(READ_FORMS)})")
        defaults = _object(data["default_form"], "a default_form table")
        _closed_keys(defaults, set(ADDRESS_SHAPES), set(ADDRESS_SHAPES), "a default_form table")
        default_form = {shape: _read_form_value(defaults[shape]) for shape in ADDRESS_SHAPES}
        budget = integral(data["budget_wire_bytes"], 0)
        if budget is None:
            raise EnvelopeError("budget_wire_bytes must be a non-negative integer")
        hint_page_size = integral(data["hint_page_size"], 1)
        if hint_page_size is None:
            raise EnvelopeError("hint_page_size must be a positive integer")
        return cls(
            forms=tuple(str(form) for form in data["forms"]),
            default_form=default_form,
            budget_wire_bytes=budget,
            hint_page_size=hint_page_size,
            discover=DiscoverCapability.from_json(data["discover"]),
            derived=_derived_vocabulary(data["derived"]),
            join=JoinCapability.from_json(data["join"]),
        )


#: ``$defs/derivedValue``'s ``rows``: one row per input row, or one per value.
DERIVED_ROWS = ("one", "one per value")

#: ``$defs/derivedShape``'s leaves: a string or an integer always present, or a
#: string, an integer or a boolean that may be ``null`` (an abstention).
DERIVED_SHAPE_LEAVES = ("string", "string|null", "integer", "integer|null", "boolean|null")


def _copied_json(value: Any) -> Any:
    """A fresh plain-JSON copy of *value*, so a published block cannot write back into what it was built from."""
    if isinstance(value, Mapping):
        return {key: _copied_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copied_json(item) for item in value]
    return value


def _derived_vocabulary(value: Any) -> tuple[Mapping[str, Any], ...]:
    """``readsCapability.derived``: an array of closed ``$defs/derivedValue`` objects.

    An empty array is admitted. A name published twice is not refused: the schema
    cannot state that uniqueness, and the parser states what the schema states.
    Each entry carries ``name``, ``at`` (``{"layer", "members"?}``), ``rows``
    (:data:`DERIVED_ROWS`) and ``shape`` (:func:`_derived_shape`).
    """
    if not _is_array(value):
        raise EnvelopeError("derived must be an array of computed-value entries")
    entries: list[Mapping[str, Any]] = []
    for entry in value:
        _object(entry, "a derived entry")
        keys = {"name", "at", "rows", "shape"}
        _closed_keys(entry, keys, keys, "a derived entry")
        name = entry["name"]
        if not isinstance(name, str) or not name:
            raise EnvelopeError("a derived entry's name is a non-empty string")
        at = _object(entry["at"], "a derived entry's at")
        _closed_keys(at, {"layer", "members"}, {"layer"}, "a derived entry's at")
        if not isinstance(at["layer"], str) or LAYER_NAME_PATTERN.fullmatch(at["layer"]) is None:
            raise EnvelopeError("a derived entry's at.layer is a layer name")
        if "members" in at and (
            not _is_array(at["members"])
            or not at["members"]
            or any(not isinstance(path, str) or MEMBERS_PATH_PATTERN.fullmatch(path) is None for path in at["members"])
        ):
            raise EnvelopeError("a derived entry's at.members is a non-empty array of member paths")
        if entry["rows"] not in DERIVED_ROWS:
            raise EnvelopeError(f"a derived entry's rows is one of {list(DERIVED_ROWS)}")
        _derived_shape(entry["shape"])
        entries.append(_copied_json(entry))
    return tuple(entries)


def _derived_shape(shape: Any) -> None:
    """``$defs/derivedShape``: a non-empty object whose every value is a leaf or another such object."""
    _object(shape, "a derived entry's shape")
    if not shape:
        raise EnvelopeError("a derived entry's shape names at least one key")
    for item in shape.values():
        if isinstance(item, str):
            if item not in DERIVED_SHAPE_LEAVES:
                raise EnvelopeError(f"a derived shape's leaf is one of {list(DERIVED_SHAPE_LEAVES)}")
        else:
            _derived_shape(item)


@dataclass(frozen=True)
class DiscoverResponse:
    """``$defs/discoverResponse`` — the passport, the token, the reads block, the inventory.

    ``layers`` is kept as the registry produced it, so the inventory has one
    definition. This class is what ``discover`` answers with
    (:func:`~swcraft_tools.editing.discovery.discover`). ``form`` is required: it
    tells a client whether the descriptor prose was stripped (``summary``) or is
    whole (``full``).
    """

    project: Passport
    baseline: str
    reads: ReadsCapability
    form: DiscoverForm
    layers: tuple[Mapping[str, Any], ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "response": "discover",
            "project": self.project.to_json(),
            "baseline": self.baseline,
            "reads": self.reads.to_json(),
            "form": self.form,
            "layers": [dict(layer) for layer in self.layers],
        }

    @classmethod
    def from_json(cls, data: Any) -> DiscoverResponse:
        _object(data, "a discoverResponse")
        keys = {"response", "project", "baseline", "reads", "form", "layers"}
        _closed_keys(data, keys, keys, "a discoverResponse")
        _const(data["response"], "discover", "response")
        if not _is_array(data["layers"]):
            raise EnvelopeError("layers must be an array of layerDescriptors")
        return cls(
            project=Passport.from_json(data["project"]),
            baseline=_revision_token(data["baseline"]),
            reads=ReadsCapability.from_json(data["reads"]),
            form=_discover_form_value(data["form"]),
            layers=tuple(_object(layer, "a layerDescriptor") for layer in data["layers"]),
        )


def _revision_token(value: Any) -> str:
    token = _non_empty_string(value, "baseline")
    if not REVISION_TOKEN_PATTERN.fullmatch(token):
        raise EnvelopeError(f"'{token}' is not a revisionToken ({REVISION_TOKEN_PATTERN.pattern})")
    return token


@dataclass(frozen=True)
class Page:
    """``$defs/page`` — where the answered window sits in the whole set.

    ``total`` is how many things the address carries after the filter and before
    the page, ``returned`` how many this response carries, ``truncated`` whether
    the two differ, and ``next`` the identity to send as the next ``after``. On the
    last page of a walk ``truncated`` is true and ``next`` is omitted.

    The cross-key arithmetic (``returned <= total``, ``truncated`` exactly
    ``returned < total``) is not enforced here: the schema cannot compare two
    properties, so a parser check would diverge from it. It is the producer's
    property (:func:`swcraft_tools.editing.read.paginate`) and tested there.
    """

    total: int
    returned: int
    truncated: bool
    next: Any = ABSENT

    def to_json(self) -> dict[str, Any]:
        page: dict[str, Any] = {"total": self.total, "returned": self.returned, "truncated": self.truncated}
        if self.next is not ABSENT:
            page["next"] = self.next
        return page

    @classmethod
    def from_json(cls, data: Any) -> Page:
        _object(data, "a page block")
        _closed_keys(data, {"total", "returned", "truncated", "next"}, {"total", "returned", "truncated"}, "a page")
        total = integral(data["total"], 0)
        if total is None:
            raise EnvelopeError(f"`page.total` is a non-negative integer, not {data['total']!r}")
        returned = integral(data["returned"], 0)
        if returned is None:
            raise EnvelopeError(f"`page.returned` is a non-negative integer, not {data['returned']!r}")
        if not isinstance(data["truncated"], bool):
            raise EnvelopeError("`page.truncated` is true or false")
        return cls(
            total=total,
            returned=returned,
            truncated=data["truncated"],
            # Any JSON value, `null` included, for `ReadRequest.after`'s reason:
            # a `whole_value` collection's identity is the member itself.
            next=data["next"] if "next" in data else ABSENT,
        )


def _page(data: Mapping[str, Any]) -> Page | None:
    """``page`` off a read response, or ``None`` when omitted; an explicit ``null`` is refused."""
    if "page" not in data:
        return None
    if data["page"] is None:
        raise EnvelopeError("`page` is a page block, or is omitted entirely")
    return Page.from_json(data["page"])


@dataclass(frozen=True)
class ReadResponse:
    """``$defs/readResponse`` — the address echoed back, the form, the filter, and the value.

    No baseline token: a read observes the model. This class is what
    :func:`~swcraft_tools.editing.read.read` answers with.

    ``form`` is always the form the answer was **published** in, not the one
    requested: a request that omitted it reads the default, and one whose form the
    address cannot express reads what was served instead.

    ``where``, ``members``, ``derived`` and ``join`` are echoed only when the
    request carried them; each is applied verbatim or refused, so an absent key
    means "not applied", not "a default was used". ``members`` is echoed because
    the slice's rows do not name the collection they came from.

    ``page`` is present only when the request carried a ``limit``, and states what
    a windowed value cannot: the total, how many came back, whether that is all, and
    where to continue. ``limit`` and ``after`` are not echoed.
    """

    address: Address
    value: Any
    form: ReadForm
    where: tuple[WhereClause, ...] = ()
    page: Page | None = None
    members: str | None = None
    derived: tuple[str, ...] = ()
    join: tuple[JoinEntry, ...] = ()

    def __post_init__(self) -> None:
        # Normalized as `ReadRequest` is, so a response built in Python equals
        # the same one parsed off the wire.
        object.__setattr__(self, "where", tuple(self.where))
        object.__setattr__(self, "derived", tuple(self.derived))
        object.__setattr__(self, "join", tuple(self.join))
        if self.members is not None:
            _checked_members(self.members)
        if self.derived:
            _derived_names(self.derived)
        check_rows(self.address, self.form, self.derived, self.join)

    def to_json(self) -> dict[str, Any]:
        response: dict[str, Any] = {
            "response": "read",
            "address": self.address.to_json(),
            "form": self.form,
        }
        if self.members is not None:
            response["members"] = self.members
        if self.where:
            response["where"] = [clause.to_json() for clause in self.where]
        if self.page is not None:
            response["page"] = self.page.to_json()
        if self.derived:
            response["derived"] = list(self.derived)
        if self.join:
            response["join"] = [entry.to_json() for entry in self.join]
        response["value"] = self.value
        return response

    @classmethod
    def from_json(cls, data: Any) -> ReadResponse:
        _object(data, "a readResponse")
        _closed_keys(
            data,
            {"response", "address", "form", "where", "page", "value", "members", "derived", "join"},
            {"response", "address", "form", "value"},
            "a readResponse",
        )
        _const(data["response"], "read", "response")
        _read_form_value(data["form"])
        return cls(
            address=_address(data["address"]),
            value=data["value"],
            form=data["form"],
            where=_where(data),
            page=_page(data),
            members=_members(data),
            derived=_derived(data),
            join=_join(data),
        )


@dataclass(frozen=True)
class DeltaEntry:
    """``$defs/deltaEntry`` — one entry of the reviewable semantic delta."""

    operation: str
    address: Address
    before: Any = ABSENT
    after: Any = ABSENT
    summary: str | None = None
    #: Who stated this change when the server completed the change set
    #: (``complete: true``): ``server`` for an entry the server's own operation
    #: produced, ``client_and_server`` for one the client's operation produced
    #: and a server operation added to. Omitted for every client-only entry.
    origin: DeltaOrigin | None = None
    #: ``"summary"`` where ``before`` is not the whole element but its ``summary``
    #: form — set at the edge, on a whole-element ``remove``, by
    #: :func:`~swcraft_tools.editing.delta.summarize_removed_elements`. ``None``
    #: (omitted on the wire) means ``before`` is whole, as it always is inside the engine.
    before_form: BeforeForm | None = None

    def __post_init__(self) -> None:
        if self.operation not in CHANGE_OPERATION_NAMES:
            raise ValueError(f"a deltaEntry operation is one of {list(CHANGE_OPERATION_NAMES)}")
        if self.origin is not None and self.origin not in DELTA_ORIGINS:
            raise ValueError(f"a deltaEntry origin is one of {list(DELTA_ORIGINS)}")
        if self.before_form is not None and self.before_form not in BEFORE_FORMS:
            raise ValueError(f"a deltaEntry before_form is one of {list(BEFORE_FORMS)}")
        if self.before_form is not None and (self.operation != "remove" or self.address.path):
            raise ValueError("a deltaEntry before_form is carried by a whole-element remove only")
        if not self.address.is_element_address:
            raise ValueError("a deltaEntry address names an element (elementAddress requires 'element')")

    def to_json(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"operation": self.operation, "address": self.address.to_json()}
        if self.before is not ABSENT:
            entry["before"] = self.before
        if self.after is not ABSENT:
            entry["after"] = self.after
        if self.summary is not None:
            entry["summary"] = self.summary
        if self.origin is not None:
            entry["origin"] = self.origin
        if self.before_form is not None:
            entry["before_form"] = self.before_form
        return entry

    @classmethod
    def from_json(cls, data: Any) -> DeltaEntry:
        _object(data, "a deltaEntry")
        _closed_keys(
            data,
            {"operation", "address", "before", "after", "summary", "origin", "before_form"},
            {"operation", "address"},
            "a deltaEntry",
        )
        origin = data.get("origin")
        if "origin" in data and origin not in DELTA_ORIGINS:
            raise EnvelopeError(f"a deltaEntry origin is one of {list(DELTA_ORIGINS)}")
        before_form = data.get("before_form")
        if "before_form" in data and before_form not in BEFORE_FORMS:
            raise EnvelopeError(f"a deltaEntry before_form is one of {list(BEFORE_FORMS)}")
        if data["operation"] not in CHANGE_OPERATION_NAMES:
            raise EnvelopeError(f"a deltaEntry operation is one of {list(CHANGE_OPERATION_NAMES)}")
        summary = data.get("summary")
        if summary is None and "summary" in data:
            # `before` and `after` admit `null` because it is a model VALUE there
            # (`ABSENT` is what says "omitted"); `summary` is prose, so the same
            # rule as `audit_ref` applies — omit the key or write a sentence.
            raise EnvelopeError("summary is a non-empty string, or is omitted entirely")
        if summary is not None:
            _non_empty_string(summary, "summary")
        return cls(
            operation=data["operation"],
            address=_element_address(data["address"], "a deltaEntry"),
            before=data.get("before", ABSENT),
            after=data.get("after", ABSENT),
            summary=summary,
            origin=origin,
            before_form=before_form,
        )


@dataclass(frozen=True)
class ChangeSetResponse:
    """``$defs/changeSetResponse`` — the outcome of an accepted change set.

    ``audit_ref`` is a security decision: **omitted** on ``staged`` (an identifier
    there would read as a commit handle, and there is no commit request) and
    **present** on ``committed`` unless the audit append itself failed. Enforced here.
    """

    status: str
    baseline: str
    delta: tuple[DeltaEntry, ...] = ()
    audit_ref: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _CHANGE_SET_STATUSES:
            raise ValueError(f"status must be one of {sorted(_CHANGE_SET_STATUSES)}, not {self.status!r}")
        if self.status == "staged" and self.audit_ref is not None:
            raise ValueError(
                "audit_ref is omitted on a `staged` response: an identifier returned there would read as a "
                "commit handle, and there is no commit request"
            )
        object.__setattr__(self, "delta", tuple(self.delta))

    def to_json(self) -> dict[str, Any]:
        response: dict[str, Any] = {
            "response": "change_set",
            "status": self.status,
            "baseline": self.baseline,
            "delta": [entry.to_json() for entry in self.delta],
        }
        if self.audit_ref is not None:
            response["audit_ref"] = self.audit_ref
        return response

    @classmethod
    def from_json(cls, data: Any) -> ChangeSetResponse:
        _object(data, "a changeSetResponse")
        _closed_keys(
            data,
            {"response", "status", "baseline", "delta", "audit_ref"},
            {"response", "status", "baseline", "delta"},
            "a changeSetResponse",
        )
        _const(data["response"], "change_set", "response")
        if not _is_declared(data["status"], _CHANGE_SET_STATUSES):
            raise EnvelopeError(f"status must be one of {sorted(_CHANGE_SET_STATUSES)}")
        if not _is_array(data["delta"]):
            raise EnvelopeError("delta must be an array of deltaEntries")
        audit_ref = data.get("audit_ref")
        if audit_ref is None and "audit_ref" in data:
            # `null` is not "omitted": `audit_ref` is a non-empty string, and a
            # `staged` response omits the key.
            raise EnvelopeError("audit_ref is a non-empty string, or is omitted entirely")
        if audit_ref is not None:
            _non_empty_string(audit_ref, "audit_ref")
        if data["status"] == "staged" and audit_ref is not None:
            raise EnvelopeError(
                "audit_ref is omitted on a `staged` response: an identifier returned there would read as a "
                "commit handle, and there is no commit request"
            )
        return cls(
            status=data["status"],
            baseline=_revision_token(data["baseline"]),
            delta=tuple(DeltaEntry.from_json(entry) for entry in data["delta"]),
            audit_ref=audit_ref,
        )


@dataclass(frozen=True)
class ReadManyResponse:
    """``$defs/readManyResponse`` — one answer per address of a batched ``read``, in order.

    Each answer is the :class:`ReadResponse` or the :class:`ErrorResult` a
    single read of that address would have earned.
    """

    answers: tuple[ReadResponse | ErrorResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "answers", tuple(self.answers))

    def to_json(self) -> dict[str, Any]:
        return {"response": "reads", "answers": [answer.to_json() for answer in self.answers]}

    @classmethod
    def from_json(cls, data: Any) -> ReadManyResponse:
        _object(data, "a readManyResponse")
        _closed_keys(data, {"response", "answers"}, {"response", "answers"}, "a readManyResponse")
        _const(data["response"], "reads", "response")
        if not _is_array(data["answers"]) or not data["answers"]:
            raise EnvelopeError("answers must be a non-empty array of readResponses and errorResults")
        answers: list[ReadResponse | ErrorResult] = []
        for answer in data["answers"]:
            _object(answer, "an answer of a readManyResponse")
            answers.append(
                ErrorResult.from_json(answer) if answer.get("response") == "error" else ReadResponse.from_json(answer)
            )
        return cls(answers=tuple(answers))


@dataclass(frozen=True)
class ModelMapResponse:
    """``$defs/modelMapResponse`` — the bounded model map: the token, every layer's count, the small layers' names.

    ``layers`` holds at most :data:`MODEL_MAP_LIST_MAX` identities per layer;
    for a layer counted and not listed, a name the task gives is resolved by
    ``find`` and a filtered or paged ``index`` read answers a question about it.
    """

    baseline: str
    counts: Mapping[str, int]
    layers: Mapping[str, tuple[Any, ...]]

    def to_json(self) -> dict[str, Any]:
        return {
            "response": "map",
            "baseline": self.baseline,
            "counts": dict(self.counts),
            "layers": {layer: list(names) for layer, names in self.layers.items()},
        }

    @classmethod
    def from_json(cls, data: Any) -> ModelMapResponse:
        _object(data, "a modelMapResponse")
        _closed_keys(
            data,
            {"response", "baseline", "counts", "layers"},
            {"response", "baseline", "counts", "layers"},
            "a modelMapResponse",
        )
        _const(data["response"], "map", "response")
        counts = dict(_object(data["counts"], "counts"))
        for layer, count in counts.items():
            counts[layer] = integral(count, 0)
            if counts[layer] is None:
                raise EnvelopeError(f"counts.{layer} must be a non-negative integer")
        layers = _object(data["layers"], "layers")
        for layer, names in layers.items():
            if not _is_array(names) or len(names) > MODEL_MAP_LIST_MAX:
                raise EnvelopeError(f"layers.{layer} must be an array of at most {MODEL_MAP_LIST_MAX} identities")
        return cls(
            baseline=_revision_token(data["baseline"]),
            counts=counts,
            layers={layer: tuple(names) for layer, names in layers.items()},
        )


@dataclass(frozen=True)
class FindResponse:
    """``$defs/findResponse`` — the baseline token and, per name in order, its candidates or ``"none"``.

    The parser holds the schema's shape and nothing more: the resolver's own
    bounds (an identity of at most 72 characters, a name of the request) are
    the engine's to keep, not a wire rule.
    """

    baseline: str
    results: tuple[Mapping[str, Any], ...]

    def to_json(self) -> dict[str, Any]:
        return {"response": "find", "baseline": self.baseline, "results": [dict(entry) for entry in self.results]}

    @classmethod
    def from_json(cls, data: Any) -> FindResponse:
        _object(data, "a findResponse")
        _closed_keys(data, {"response", "baseline", "results"}, {"response", "baseline", "results"}, "a findResponse")
        _const(data["response"], "find", "response")
        if not _is_array(data["results"]):
            raise EnvelopeError("results must be an array")
        results = []
        for entry in data["results"]:
            _object(entry, "a find result")
            _closed_keys(entry, {"name", "candidates", "more"}, {"name", "candidates"}, "a find result")
            if not isinstance(entry["name"], str):
                raise EnvelopeError("a find result's name must be a string")
            candidates = entry["candidates"]
            if candidates != "none":
                if not _is_array(candidates) or len(candidates) > CANDIDATES_PER_NAME:
                    raise EnvelopeError(
                        f'candidates must be "none" or an array of at most {CANDIDATES_PER_NAME} candidates'
                    )
                for candidate in candidates:
                    _object(candidate, "a candidate")
                    _closed_keys(
                        candidate, {"address", "tier", "identity"}, {"address", "tier", "identity"}, "a candidate"
                    )
                    _address(candidate["address"])
                    if not _is_declared(candidate["tier"], CANDIDATE_TIERS):
                        raise EnvelopeError(f"a candidate's tier is one of {', '.join(CANDIDATE_TIERS)}")
                    if not isinstance(candidate["identity"], str):
                        raise EnvelopeError("a candidate's identity must be a string")
            if "more" in entry:
                more = integral(entry["more"], 1)
                if more is None:
                    raise EnvelopeError("more must be a positive integer")
                entry = dict(entry) | {"more": more}
            results.append(entry)
        return cls(baseline=_revision_token(data["baseline"]), results=tuple(results))


@dataclass(frozen=True)
class FindTextResponse:
    """``$defs/findTextResponse`` — the baseline token, the text echoed, the matching requirements or ``"none"``.

    Each candidate is a requirement's address, its one-line identity and the
    words of its text the query's words matched, in the query's order; at most
    :data:`FIND_TEXT_MAX`, in ``id`` order, ``more`` counting the rest.
    """

    baseline: str
    text: str
    candidates: tuple[Mapping[str, Any], ...] | str
    more: int | None = None

    def to_json(self) -> dict[str, Any]:
        response: dict[str, Any] = {
            "response": "find",
            "baseline": self.baseline,
            "text": self.text,
            "candidates": self.candidates
            if isinstance(self.candidates, str)
            else [dict(candidate) for candidate in self.candidates],
        }
        if self.more is not None:
            response["more"] = self.more
        return response

    @classmethod
    def from_json(cls, data: Any) -> FindTextResponse:
        _object(data, "a findTextResponse")
        _closed_keys(
            data,
            {"response", "baseline", "text", "candidates", "more"},
            {"response", "baseline", "text", "candidates"},
            "a findTextResponse",
        )
        _const(data["response"], "find", "response")
        if not isinstance(data["text"], str):
            raise EnvelopeError("a findTextResponse's text must be a string")
        candidates = data["candidates"]
        if candidates != "none":
            if not _is_array(candidates) or not candidates or len(candidates) > FIND_TEXT_MAX:
                raise EnvelopeError(f'candidates must be "none" or an array of 1 to {FIND_TEXT_MAX} candidates')
            for candidate in candidates:
                _object(candidate, "a candidate")
                _closed_keys(
                    candidate, {"address", "identity", "words"}, {"address", "identity", "words"}, "a candidate"
                )
                _address(candidate["address"])
                if not isinstance(candidate["identity"], str):
                    raise EnvelopeError("a candidate's identity must be a string")
                words = candidate["words"]
                if not _is_array(words) or not words or not all(isinstance(word, str) for word in words):
                    raise EnvelopeError("a candidate's words must be a non-empty array of strings")
        more = integral(data["more"], 1) if "more" in data else None
        if "more" in data and more is None:
            raise EnvelopeError("more must be a positive integer")
        return cls(
            baseline=_revision_token(data["baseline"]),
            text=data["text"],
            candidates=candidates if candidates == "none" else tuple(candidates),
            more=more,
        )


def _figure(value: Any, where: str) -> dict[str, int]:
    """A score figure ``{"n", "d"}`` of two integers, as the trace search writes one; normalised."""
    _object(value, where)
    _closed_keys(value, {"n", "d"}, {"n", "d"}, where)
    n, d = integral(value["n"]), integral(value["d"])
    if n is None or d is None:
        raise EnvelopeError(f"{where}'s n and d must be integers")
    return {"n": n, "d": d}


def _strings(value: Any) -> bool:
    return _is_array(value) and all(isinstance(item, str) for item in value)


#: The trace search's closed evidence records, by ``kind``: the keys each carries
#: (``trace_search._records``' own, held equal to it by a test).
_EVIDENCE_KEYS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "name": frozenset({"kind", "phrase"}),
        "neighbours": frozenset({"kind", "share", "seeded", "of", "examples"}),
        "similar": frozenset({"kind", "score", "count", "examples"}),
        "text": frozenset({"kind", "terms"}),
    }
)


def _evidence(records: Any, where: str) -> list[dict[str, Any]]:
    """An evidence list: the trace search's closed records, each of one ``kind``; its integers normalised."""
    if not _is_array(records):
        raise EnvelopeError(f"{where}'s evidence must be an array")
    parsed = []
    for record in records:
        _object(record, f"{where}'s evidence record")
        kind = record.get("kind")
        keys = _EVIDENCE_KEYS.get(kind) if isinstance(kind, str) else None
        if keys is None:
            raise EnvelopeError(f"{where}'s evidence record kind must be one of {list(_EVIDENCE_KEYS)}")
        _closed_keys(record, set(keys), set(keys), f"{where}'s evidence record")
        if kind == "name" and not isinstance(record["phrase"], str):
            raise EnvelopeError("a name record's phrase must be a string")
        if kind == "text" and not _strings(record["terms"]):
            raise EnvelopeError("a text record's terms must be an array of strings")
        record = dict(record)
        if kind == "neighbours":
            share = integral(record["share"])
            record["share"] = _figure(record["share"], "a neighbours record's share") if share is None else share
            seeded, of = integral(record["seeded"]), integral(record["of"])
            if seeded is None or of is None or not _strings(record["examples"]):
                raise EnvelopeError("a neighbours record's seeded and of must be integers, its examples strings")
            record |= {"seeded": seeded, "of": of}
        if kind == "similar":
            record["score"] = _figure(record["score"], "a similar record's score")
            count = integral(record["count"])
            if count is None or not _strings(record["examples"]):
                raise EnvelopeError("a similar record's count must be an integer, its examples strings")
            record["count"] = count
        parsed.append(record)
    return parsed


def _target(value: Any, where: str) -> None:
    """A trace search target address ``{kind, owner, name}``."""
    _object(value, where)
    _closed_keys(value, {"kind", "owner", "name"}, {"kind", "owner", "name"}, where)
    if not isinstance(value["kind"], str) or not isinstance(value["name"], str):
        raise EnvelopeError(f"{where}'s kind and name must be strings")
    if value["owner"] is not None and not isinstance(value["owner"], str):
        raise EnvelopeError(f"{where}'s owner must be a string or null")


def _candidate_place(place: Any) -> dict[str, Any]:
    """A place of a ``find`` for a requirement: its group, score, evidence and non-empty rows."""
    keys = {"group", "score", "evidence", "rows"}
    _object(place, "a place")
    _closed_keys(place, keys, keys, "a place")
    _object(place["group"], "a place's group")
    _closed_keys(place["group"], {"kind", "name"}, {"kind", "name"}, "a place's group")
    if not all(isinstance(place["group"][key], str) for key in ("kind", "name")):
        raise EnvelopeError("a place's group kind and name must be strings")
    score = _figure(place["score"], "a place's score")
    evidence = _evidence(place["evidence"], "a place")
    rows = place["rows"]
    if not _is_array(rows) or not rows:
        raise EnvelopeError("a place's rows must be a non-empty array")
    return dict(place) | {"score": score, "evidence": evidence, "rows": [_candidate_row(row) for row in rows]}


def _candidate_row(row: Any) -> dict[str, Any]:
    """A place's row: the protocol address, the search's target, its score, band and evidence."""
    keys = {"address", "target", "score", "band", "evidence"}
    _object(row, "a row")
    _closed_keys(row, keys, keys, "a row")
    _address(row["address"])
    _target(row["target"], "a row's target")
    score = _figure(row["score"], "a row's score")
    if row["band"] not in FIND_CANDIDATE_BANDS:
        raise EnvelopeError(f"a row's band must be one of {list(FIND_CANDIDATE_BANDS)}")
    return dict(row) | {"score": score, "evidence": _evidence(row["evidence"], "a row")}


def _existing_link(link: Any) -> dict[str, Any]:
    """A link the requirement already has: its address, target, member, stated version and whether it is confirmed."""
    keys = {"address", "target", "member", "version", "confirmed"}
    _object(link, "an existing link")
    _closed_keys(link, keys, keys, "an existing link")
    _address(link["address"])
    _target(link["target"], "an existing link's target")
    if not isinstance(link["member"], str) or not isinstance(link["confirmed"], bool):
        raise EnvelopeError("an existing link's member must be a string and confirmed a boolean")
    version = None if link["version"] is None else integral(link["version"])
    if link["version"] is not None and version is None:
        raise EnvelopeError("an existing link's version must be an integer or null")
    return dict(link) | {"version": version}


_LINKS_LEFT_OUT = (
    "existing_links_more and existing_links_read are carried together: the count of the links left out "
    "and the read that pages them"
)
_LINKS_LISTED_IN_FULL = (
    f"existing_links_more is carried beside exactly {FIND_EXISTING_LINKS_MAX} listed links: links are left out "
    "only once the list is full"
)


@dataclass(frozen=True)
class FindCandidatesResponse:
    """``$defs/findCandidatesResponse`` — the trace search's ranking for one requirement.

    The baseline token, the requirement echoed, its document's linked neighbours
    (the ranking's ``n``), the places best first or ``"none"``, and the links it
    already has. Each place is a target group with its score, evidence and rows;
    each row the protocol ``address`` that would carry the link, the search's
    ``target``, its score, band and evidence. Projected one to one from
    :class:`~swcraft_tools.trace_search.Ranking`, never re-ordered.

    At most :data:`FIND_EXISTING_LINKS_MAX` existing links are listed; when links
    were left out, ``existing_links_more`` counts them and ``existing_links_read`` is
    the ``read`` that pages them all, sent back verbatim. The two keys travel
    together, and only beside a full list.
    """

    baseline: str
    for_requirement: str
    neighbours: int
    places: tuple[Mapping[str, Any], ...] | str
    existing_links: tuple[Mapping[str, Any], ...]
    existing_links_more: int | None = None
    existing_links_read: ReadRequest | None = None

    def __post_init__(self) -> None:
        if (self.existing_links_more is None) is not (self.existing_links_read is None):
            raise EnvelopeError(_LINKS_LEFT_OUT)
        if self.existing_links_more is not None and len(self.existing_links) != FIND_EXISTING_LINKS_MAX:
            raise EnvelopeError(_LINKS_LISTED_IN_FULL)

    def to_json(self) -> dict[str, Any]:
        response: dict[str, Any] = {
            "response": "find",
            "baseline": self.baseline,
            "for_requirement": self.for_requirement,
            "neighbours": self.neighbours,
            "places": self.places if isinstance(self.places, str) else [dict(place) for place in self.places],
            "existing_links": [dict(link) for link in self.existing_links],
        }
        if self.existing_links_read is not None:  # `__post_init__` carries the count beside it
            response["existing_links_more"] = self.existing_links_more
            response["existing_links_read"] = self.existing_links_read.to_json()
        return response

    @classmethod
    def from_json(cls, data: Any) -> FindCandidatesResponse:
        required = {"response", "baseline", "for_requirement", "neighbours", "places", "existing_links"}
        keys = required | {"existing_links_more", "existing_links_read"}
        _object(data, "a findCandidatesResponse")
        _closed_keys(data, keys, required, "a findCandidatesResponse")
        _const(data["response"], "find", "response")
        if not isinstance(data["for_requirement"], str):
            raise EnvelopeError("a findCandidatesResponse's for_requirement must be a string")
        neighbours = integral(data["neighbours"], 0)
        if neighbours is None:
            raise EnvelopeError("neighbours must be a non-negative integer")
        places = data["places"]
        if places != "none":
            if not _is_array(places) or not places or len(places) > FIND_CANDIDATES_MAX:
                raise EnvelopeError(f'places must be "none" or an array of 1 to {FIND_CANDIDATES_MAX} places')
            places = [_candidate_place(place) for place in places]
        links = data["existing_links"]
        if not _is_array(links) or len(links) > FIND_EXISTING_LINKS_MAX:
            raise EnvelopeError(f"existing_links must be an array of at most {FIND_EXISTING_LINKS_MAX} links")
        links = [_existing_link(link) for link in links]
        # Each key parsed when present; `__post_init__` refuses one without the other.
        more = read = None
        if "existing_links_more" in data:
            more = integral(data["existing_links_more"], 1)
            if more is None:
                raise EnvelopeError("existing_links_more must be a positive integer")
        if "existing_links_read" in data:
            read = ReadRequest.from_json(data["existing_links_read"])
        return cls(
            baseline=_revision_token(data["baseline"]),
            for_requirement=data["for_requirement"],
            neighbours=neighbours,
            places=places if places == "none" else tuple(places),
            existing_links=tuple(links),
            existing_links_more=more,
            existing_links_read=read,
        )


def _parse_find_response(data: Any) -> FindResponse | FindTextResponse | FindCandidatesResponse:
    """A ``find`` answer in the form its request took: ``text`` or ``for_requirement`` echoed, ``results`` otherwise."""
    _object(data, "a findResponse")
    if "for_requirement" in data:
        return FindCandidatesResponse.from_json(data)
    if "text" in data:
        return FindTextResponse.from_json(data)
    return FindResponse.from_json(data)


@dataclass(frozen=True)
class CountResponse:
    """``$defs/countResponse`` — the address and the set keys a count read echoes, and the count."""

    address: Address
    count: int
    where: tuple[WhereClause, ...] = ()
    members: str | None = None
    derived: tuple[str, ...] = ()
    join: tuple[JoinEntry, ...] = ()

    def to_json(self) -> dict[str, Any]:
        response: dict[str, Any] = {"response": "count", "address": self.address.to_json(), "count": self.count}
        if self.members is not None:
            response["members"] = self.members
        if self.where:
            response["where"] = [clause.to_json() for clause in self.where]
        if self.derived:
            response["derived"] = list(self.derived)
        if self.join:
            response["join"] = [entry.to_json() for entry in self.join]
        return response

    @classmethod
    def from_json(cls, data: Any) -> CountResponse:
        _object(data, "a countResponse")
        _closed_keys(
            data,
            {"response", "address", "count", "where", "members", "derived", "join"},
            {"response", "address", "count"},
            "a countResponse",
        )
        _const(data["response"], "count", "response")
        count = integral(data["count"], 0)
        if count is None:
            raise EnvelopeError("count must be a non-negative integer")
        return cls(
            address=_address(data["address"]),
            count=count,
            where=_where(data),
            members=_members(data),
            derived=_derived(data),
            join=_join(data),
        )


Response = (
    DiscoverResponse
    | ReadResponse
    | ReadManyResponse
    | ModelMapResponse
    | FindResponse
    | FindTextResponse
    | FindCandidatesResponse
    | CountResponse
    | ChangeSetResponse
    | ErrorResult
)

_RESPONSE_PARSERS: Mapping[str, Any] = MappingProxyType(
    {
        "discover": DiscoverResponse.from_json,
        "read": ReadResponse.from_json,
        "reads": ReadManyResponse.from_json,
        "map": ModelMapResponse.from_json,
        "find": _parse_find_response,
        "count": CountResponse.from_json,
        "change_set": ChangeSetResponse.from_json,
        "error": ErrorResult.from_json,
    }
)


def parse_response(data: Any) -> Response:
    """The parsed ``$defs/response``, dispatched on its ``response`` discriminator.

    Raises:
        EnvelopeError: the discriminator is absent or outside the closed set, or
            the body does not match the shape it names.
    """
    _object(data, "a response")
    if "response" not in data:
        raise EnvelopeError(f"a response requires ['response'], one of {sorted(_RESPONSE_PARSERS)}")
    name = data["response"]
    if not _is_declared(name, _RESPONSE_PARSERS):
        raise EnvelopeError(f"'{name}' is not a protocol response shape")
    return _RESPONSE_PARSERS[name](data)

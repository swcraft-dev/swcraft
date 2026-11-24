"""Typed, exact, and strictly validated specimen generator presets."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, NoReturn

import yaml
from yaml.nodes import ScalarNode

from .arithmetic import milliseconds_to_microseconds
from .errors import PresetError
from .names import validate_unique_identifiers
from .specimen.preset import (
    SPECIMEN_KIND,
    SpecimenPreset,
    load_yaml_text,
    specimen_from_value,
    yaml_int,
    yaml_mapping,
)


class _Loader(yaml.SafeLoader):
    pass


def _decimal(loader: yaml.SafeLoader, node: ScalarNode) -> Decimal:
    return Decimal(loader.construct_scalar(node))


_Loader.add_constructor("tag:yaml.org,2002:float", _decimal)
_Loader.add_constructor("tag:yaml.org,2002:int", yaml_int)
_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, yaml_mapping)


# `StrEnum` would change `str()` and `format()` of a member, which the generator's output may read.
class ASIL(str, Enum):  # noqa: UP042
    ABSENT = "absent"
    QM = "QM"
    A = "A"
    B = "B"
    C = "C"
    D = "D"


@dataclass(frozen=True)
class IntegerRange:
    minimum: int
    maximum: int


@dataclass(frozen=True)
class WeightedValue:
    value: int
    weight: int


@dataclass(frozen=True)
class ExecutionTimeParameters:
    minimum_us: Decimal
    average_us: Decimal
    maximum_us: Decimal


@dataclass(frozen=True)
class Core:
    name: str
    capability: ASIL


@dataclass(frozen=True)
class Memory:
    size_bytes: int
    threshold: Decimal


@dataclass(frozen=True)
class Memories:
    shared: Memory
    private_per_core: Memory


@dataclass(frozen=True)
class Preset:
    name: str
    seed: int
    components: int
    domains: int
    cores: tuple[Core, ...]
    core_threshold: Decimal
    memories: Memories
    periods_ms: tuple[tuple[Decimal, int], ...]
    periods_us: tuple[tuple[int, int], ...]
    runnables_per_component: IntegerRange
    wcet_us_by_period: tuple[tuple[int, ExecutionTimeParameters], ...]
    signal_size_bytes: tuple[WeightedValue, ...]
    intra_domain_link_density: Decimal
    inter_domain_link_density: Decimal
    client_server_share: Decimal
    asil_weights: tuple[tuple[ASIL, int], ...]
    displaced_components: int
    private_memory_share: Decimal
    target_core_load: Decimal
    task_overhead_us: int
    emit_runnable_wcet: bool


ROOT_KEYS = {
    "name",
    "seed",
    "components",
    "domains",
    "cores",
    "core_threshold",
    "memories",
    "periods_ms",
    "runnables_per_component",
    "wcet_us_by_period",
    "signal_size_bytes",
    "intra_domain_link_density",
    "inter_domain_link_density",
    "client_server_share",
    "asil_weights",
    "displaced_components",
    "private_memory_share",
    "target_core_load",
    "task_overhead_us",
    "emit_runnable_wcet",
}


def _fail(path: str, message: str) -> NoReturn:
    raise PresetError(f"{path}: {message}")


def _map(value: Any, path: str) -> dict[Any, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be a mapping")
    return value


def _keys(value: dict[Any, Any], allowed: set[str], path: str) -> None:
    for key in value:
        if not isinstance(key, str) or key not in allowed:
            _fail(f"{path}.{key}", "unknown key")


def _required(value: dict[Any, Any], key: str, path: str) -> Any:
    if key not in value:
        _fail(f"{path}.{key}", "required field is missing")
    return value[key]


def _integer(value: Any, path: str, *, positive: bool = False, nonnegative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "must be an integer")
    if positive and value <= 0:
        _fail(path, "must be positive")
    if nonnegative and value < 0:
        _fail(path, "must be non-negative")
    return int(value)


def _decimal_value(value: Any, path: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, str)):
        _fail(path, "must be a decimal number")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        _fail(path, "must be a decimal number")
    if not result.is_finite():
        _fail(path, "must be finite")
    return result


def _fraction(value: Any, path: str) -> Decimal:
    result = _decimal_value(value, path)
    if result < 0 or result > 1:
        _fail(path, "must be in [0, 1]")
    return result


def _positive_fraction(value: Any, path: str) -> Decimal:
    result = _fraction(value, path)
    if result <= 0:
        _fail(path, "must be in (0, 1]")
    return result


def _asil(value: Any, path: str, *, allow_absent: bool = True) -> ASIL:
    if value is None and allow_absent:
        return ASIL.ABSENT
    if not isinstance(value, str):
        _fail(path, "must be one of absent, QM, A, B, C, D")
    token = value.lower() if value.lower() == "absent" else value
    try:
        return ASIL(token)
    except ValueError:
        _fail(path, "invalid ASIL token; expected absent, QM, A, B, C, or D")


def _range(value: Any, path: str) -> IntegerRange:
    mapping = _map(value, path)
    _keys(mapping, {"min", "max"}, path)
    result = IntegerRange(
        _integer(_required(mapping, "min", path), f"{path}.min", positive=True),
        _integer(_required(mapping, "max", path), f"{path}.max", positive=True),
    )
    if result.minimum > result.maximum:
        _fail(path, "min must be less than or equal to max")
    return result


def _weighted(value: Any, path: str, *, positive_values: bool) -> tuple[WeightedValue, ...]:
    mapping = _map(value, path)
    if not mapping:
        _fail(path, "must not be empty")
    entries: list[WeightedValue] = []
    for raw_value, raw_weight in mapping.items():
        value_int = _integer(raw_value, f"{path}.{raw_value}", positive=positive_values)
        weight = _integer(raw_weight, f"{path}.{raw_value}", nonnegative=True)
        entries.append(WeightedValue(value_int, weight))
    if sum(entry.weight for entry in entries) <= 0:
        _fail(path, "sum of weights must be positive")
    return tuple(entries)


def _load_value(path: Path) -> Any:
    if not path.exists():
        raise PresetError(f"preset not found: {path}")
    if not path.is_file():
        raise PresetError(f"preset is not a regular file: {path}")
    try:
        with path.open("r", encoding="utf-8") as stream:
            return yaml.load(stream, Loader=_Loader)
    except (OSError, UnicodeError) as exc:
        raise PresetError(f"cannot read preset {path}: {exc}") from None
    except PresetError:
        raise
    except RecursionError:
        raise PresetError(f"invalid YAML in preset {path}: it nests deeper than the YAML reader follows") from None
    except yaml.YAMLError as exc:
        detail = getattr(exc, "problem", None) or "invalid YAML"
        raise PresetError(f"invalid YAML in preset {path}: {detail}") from None


def load_preset(path: Path) -> Preset:
    value = _load_value(path)
    if not isinstance(value, dict):
        raise PresetError(f"preset root must be a mapping: {path}")
    _keys(value, ROOT_KEYS, "preset")
    name = _required(value, "name", "preset")
    if not isinstance(name, str) or not name:
        _fail("name", "must be a non-empty string")
    seed = _integer(_required(value, "seed", "preset"), "seed")
    components = _integer(_required(value, "components", "preset"), "components", positive=True)
    domains = _integer(_required(value, "domains", "preset"), "domains", positive=True)
    if domains > components:
        _fail("domains", "must be less than or equal to components")

    raw_cores = _required(value, "cores", "preset")
    if not isinstance(raw_cores, list) or not raw_cores:
        _fail("cores", "must be a non-empty list")
    cores: list[Core] = []
    for index, raw_core in enumerate(raw_cores):
        core_path = f"cores[{index}]"
        mapping = _map(raw_core, core_path)
        _keys(mapping, {"name", "asil_capability"}, core_path)
        core_name = _required(mapping, "name", core_path)
        if not isinstance(core_name, str) or not core_name:
            _fail(f"{core_path}.name", "must be a non-empty string")
        cores.append(
            Core(
                core_name,
                _asil(_required(mapping, "asil_capability", core_path), f"{core_path}.asil_capability"),
            )
        )
    try:
        validate_unique_identifiers((core.name for core in cores), "core names")
    except ValueError as exc:
        _fail("cores", str(exc))

    memories = _map(_required(value, "memories", "preset"), "memories")
    _keys(memories, {"shared", "private_per_core"}, "memories")
    parsed_memories: dict[str, Memory] = {}
    for memory_name in ("shared", "private_per_core"):
        memory_path = f"memories.{memory_name}"
        mapping = _map(_required(memories, memory_name, "memories"), memory_path)
        _keys(mapping, {"size", "threshold"}, memory_path)
        parsed_memories[memory_name] = Memory(
            _integer(_required(mapping, "size", memory_path), f"{memory_path}.size", positive=True),
            _decimal_value(_required(mapping, "threshold", memory_path), f"{memory_path}.threshold"),
        )
        if not 0 < parsed_memories[memory_name].threshold <= 1:
            _fail(f"{memory_path}.threshold", "must be in (0, 1]")

    periods_raw = _map(_required(value, "periods_ms", "preset"), "periods_ms")
    if not periods_raw:
        _fail("periods_ms", "must not be empty")
    periods: list[tuple[Decimal, int]] = []
    for raw_period, raw_weight in periods_raw.items():
        period_path = f"periods_ms.{raw_period}"
        period = _decimal_value(raw_period, period_path)
        if period <= 0:
            _fail(period_path, "must be positive")
        periods.append((period, _integer(raw_weight, period_path, nonnegative=True)))
    if sum(weight for _, weight in periods) <= 0:
        _fail("periods_ms", "sum of weights must be positive")
    periods_us: list[tuple[int, int]] = []
    for period, weight in periods:
        try:
            period_us = milliseconds_to_microseconds(period)
        except ValueError as exc:
            _fail(f"periods_ms.{period}", str(exc))
        if 1_000_000 % period_us != 0:
            _fail(f"periods_ms.{period}", "does not provide an exact integer byte rate")
        periods_us.append((period_us, weight))

    runnable_range = _range(_required(value, "runnables_per_component", "preset"), "runnables_per_component")
    execution_raw = _map(_required(value, "wcet_us_by_period", "preset"), "wcet_us_by_period")
    execution: list[tuple[int, ExecutionTimeParameters]] = []
    seen_keys: set[int] = set()
    for raw_key, raw_entry in execution_raw.items():
        key_path = f"wcet_us_by_period.{raw_key}"
        try:
            period_us = milliseconds_to_microseconds(_decimal_value(raw_key, key_path))
        except ValueError as exc:
            _fail(key_path, str(exc))
        if period_us in seen_keys:
            _fail(key_path, "duplicate period")
        seen_keys.add(period_us)
        mapping = _map(raw_entry, key_path)
        _keys(mapping, {"min", "average", "max"}, key_path)
        values = tuple(
            _decimal_value(_required(mapping, field, key_path), f"{key_path}.{field}")
            for field in ("min", "average", "max")
        )
        if any(item <= 0 for item in values) or not values[0] <= values[1] <= values[2]:
            _fail(key_path, "execution-time values must be positive and min <= average <= max")
        execution.append((period_us, ExecutionTimeParameters(*values)))
    expected_periods = {period_us for period_us, _ in periods_us}
    if expected_periods != seen_keys:
        _fail("wcet_us_by_period", "keys must match periods_ms exactly")

    signal_sizes = _weighted(_required(value, "signal_size_bytes", "preset"), "signal_size_bytes", positive_values=True)
    densities = tuple(
        _fraction(_required(value, field, "preset"), field)
        for field in (
            "intra_domain_link_density",
            "inter_domain_link_density",
            "client_server_share",
            "private_memory_share",
            "target_core_load",
        )
    )
    asil_raw = _map(_required(value, "asil_weights", "preset"), "asil_weights")
    asil_weights: list[tuple[ASIL, int]] = []
    for token, weight in asil_raw.items():
        asil = _asil(token, f"asil_weights.{token}", allow_absent=False)
        asil_weights.append((asil, _integer(weight, f"asil_weights.{token}", nonnegative=True)))
    if not asil_weights or sum(weight for _, weight in asil_weights) <= 0:
        _fail("asil_weights", "sum of weights must be positive")
    displaced = _integer(_required(value, "displaced_components", "preset"), "displaced_components", nonnegative=True)
    if displaced >= components:
        _fail("displaced_components", "must be less than components")
    overhead = _integer(_required(value, "task_overhead_us", "preset"), "task_overhead_us", positive=True)
    emit = _required(value, "emit_runnable_wcet", "preset")
    if not isinstance(emit, bool):
        _fail("emit_runnable_wcet", "must be a boolean")
    return Preset(
        name,
        seed,
        components,
        domains,
        tuple(cores),
        _positive_fraction(_required(value, "core_threshold", "preset"), "core_threshold"),
        Memories(parsed_memories["shared"], parsed_memories["private_per_core"]),
        tuple(periods),
        tuple(periods_us),
        runnable_range,
        tuple(execution),
        signal_sizes,
        densities[0],
        densities[1],
        densities[2],
        tuple(asil_weights),
        displaced,
        densities[3],
        densities[4],
        overhead,
        emit,
    )


def load_any_preset(path: Path) -> Preset | SpecimenPreset:
    """Load a preset of either kind, dispatching on its root key `kind`.

    A preset without `kind` is a WATERS preset, read by `load_preset`; with `kind: specimen` it is
    a specimen preset; any other kind is refused. Text that is not a YAML mapping at all goes to
    `load_preset`, so a WATERS preset keeps its own errors.
    """
    try:
        value = load_yaml_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, RecursionError, yaml.YAMLError, PresetError):
        return load_preset(path)
    if not isinstance(value, dict) or "kind" not in value:
        return load_preset(path)
    if value["kind"] != SPECIMEN_KIND:
        raise PresetError(f"kind: unknown preset kind; the known kind is {SPECIMEN_KIND!r}")
    return specimen_from_value(value)

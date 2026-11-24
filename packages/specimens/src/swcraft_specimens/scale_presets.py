"""Test-only feasible calibration presets (not production presets)."""

from decimal import Decimal

from .preset import (
    ASIL,
    Core,
    ExecutionTimeParameters,
    IntegerRange,
    Memories,
    Memory,
    Preset,
    WeightedValue,
)


def scale_preset(components: int, domains: int, cores: int, displaced: int, seed: int = 20260921) -> Preset:
    if (components, domains, cores, displaced) not in (
        (15, 3, 3, 3),
        (60, 6, 3, 8),
        (160, 8, 3, 20),
    ):
        raise ValueError("the supported test scales are 15/3/3/3, 60/6/3/8, and 160/8/3/20")
    periods = (
        (1000, 3),
        (2000, 2),
        (5000, 2),
        (10000, 25),
        (20000, 25),
        (50000, 3),
        (100000, 20),
        (200000, 1),
        (1000000, 4),
    )
    execution = tuple(
        (period, ExecutionTimeParameters(Decimal(1), Decimal(100), Decimal(500))) for period, _ in periods
    )
    return Preset(
        f"scale_{components}",
        seed,
        components,
        domains,
        tuple(Core(f"CORE{i}", ASIL.D) for i in range(cores)),
        Decimal("0.7"),
        Memories(Memory(10_000_000, Decimal("0.8")), Memory(10_000_000, Decimal("0.8"))),
        tuple((Decimal(period) / Decimal(1000), weight) for period, weight in periods),
        periods,
        IntegerRange(1, 6),
        execution,
        tuple(
            WeightedValue(value, weight)
            for value, weight in (
                (1, 35),
                (2, 49),
                (4, 13),
                (5, 1),
                (9, 1),
                (17, 1),
                (33, 1),
                (65, 1),
            )
        ),
        Decimal("0.2"),
        Decimal("0.03"),
        Decimal("0.1"),
        ((ASIL.B, 1),),
        displaced,
        Decimal("0.1"),
        Decimal("0.1"),
        5,
        False,
    )

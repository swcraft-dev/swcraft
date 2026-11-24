"""Exact arithmetic primitives used by the specimen generator."""

from decimal import Decimal
from fractions import Fraction


def milliseconds_to_microseconds(value: Decimal | int | str) -> int:
    """Convert decimal milliseconds to an exact integral number of microseconds."""
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    microseconds = decimal * Decimal(1000)
    if microseconds != microseconds.to_integral_value():
        raise ValueError(f"period is not an exact integer number of microseconds: {value}")
    return int(microseconds)


def cpu_load(wcet_us: int, period_us: int) -> Fraction:
    if wcet_us < 0 or period_us <= 0:
        raise ValueError("wcet_us must be non-negative and period_us must be positive")
    return Fraction(wcet_us, period_us)


def sum_cpu_loads(loads: list[Fraction] | tuple[Fraction, ...]) -> Fraction:
    return sum(loads, Fraction(0))


def sender_receiver_rate(payload_bytes: int, period_us: int) -> Fraction:
    if payload_bytes < 0 or period_us <= 0:
        raise ValueError("payload_bytes must be non-negative and period_us must be positive")
    return Fraction(payload_bytes * 1_000_000, period_us)


def client_server_rate(request_bytes: int, response_bytes: int, caller_period_us: int) -> Fraction:
    if request_bytes < 0 or response_bytes < 0 or caller_period_us <= 0:
        raise ValueError("payload sizes must be non-negative and caller_period_us must be positive")
    return Fraction((request_bytes + response_bytes) * 1_000_000, caller_period_us)


def require_exact_byte_rate(rate: Fraction, context: str = "byte rate") -> int:
    """Return an integer rate, rejecting platform-dependent fractional rates."""
    if rate.denominator != 1:
        raise ValueError(f"{context} is not exactly representable as bytes per second: {rate}")
    return rate.numerator

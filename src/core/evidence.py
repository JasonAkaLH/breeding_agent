from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def non_negative_number(value: Any) -> bool:
    return is_number(value) and value >= 0


def number_at_least(value: Any, lower_bound: int | float) -> bool:
    return is_number(value) and value >= lower_bound


def number_at_most(value: Any, upper_bound: Any) -> bool:
    return is_number(value) and is_number(upper_bound) and value <= upper_bound


def require_boolean_evidence(
    evidence: Any,
    required_items: Iterable[str],
    error_factory: Callable[[], None],
) -> None:
    if not isinstance(evidence, Mapping) or any(evidence.get(item) is not True for item in required_items):
        error_factory()

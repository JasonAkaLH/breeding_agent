from __future__ import annotations


class CoreError(Exception):
    """Base exception for shared core-layer failures."""


class ContractValidationError(CoreError):
    """Raised when a shared contract is malformed or inconsistent."""


class BoundaryViolationError(CoreError):
    """Raised when code crosses a forbidden module boundary."""

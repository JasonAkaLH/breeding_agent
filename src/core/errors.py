from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class CoreError(Exception):
    """Base exception for shared core-layer failures."""


class ContractValidationError(CoreError):
    """Raised when a shared contract is malformed or inconsistent."""


class BoundaryViolationError(CoreError):
    """Raised when code crosses a forbidden module boundary."""


class RustCoreContractError(ContractValidationError):
    """Raised when the Rust Core contract or PyO3 facade fails closed."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "core_contract_mismatch",
        category: str = "contract",
        retriable: bool = False,
        safe_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.category = category
        self.retriable = retriable
        self.safe_metadata = {str(key): str(value) for key, value in dict(safe_metadata or {}).items()}

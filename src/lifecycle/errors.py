from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.core.errors import CoreError


class LifecycleError(CoreError):
    """Base exception for lifecycle-layer failures."""


class LifecycleTransitionError(LifecycleError):
    """Raised when an invalid lifecycle state transition is attempted."""


class ConversationBusyError(LifecycleError):
    """Raised when a conversation already has an active task."""


class LifecycleRustContractError(LifecycleError):
    """Raised when the Rust Lifecycle contract or PyO3 facade fails closed."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "lifecycle_contract_mismatch",
        category: str = "lifecycle",
        retriable: bool = False,
        safe_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.category = category
        self.retriable = retriable
        self.safe_metadata = {str(key): str(value) for key, value in dict(safe_metadata or {}).items()}

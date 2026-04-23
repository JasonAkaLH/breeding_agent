from __future__ import annotations

from src.core.errors import CoreError


class LifecycleError(CoreError):
    """Base exception for lifecycle-layer failures."""


class LifecycleTransitionError(LifecycleError):
    """Raised when an invalid lifecycle state transition is attempted."""


class ConversationBusyError(LifecycleError):
    """Raised when a conversation already has an active task."""

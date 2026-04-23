from __future__ import annotations


class BackpressureRejected(RuntimeError):
    """Raised when strict reject backpressure denies a new orchestration request."""


class BackpressureGuard:
    def __init__(self, *, max_active_tasks: int) -> None:
        self._max_active_tasks = max_active_tasks

    def ensure_can_accept(self, *, active_task_count: int) -> None:
        if active_task_count >= self._max_active_tasks:
            raise BackpressureRejected(
                f"Strict reject backpressure triggered: active_task_count={active_task_count}, limit={self._max_active_tasks}."
            )

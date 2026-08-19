from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from src.core.contracts import StoragePort
from src.core.models import MCPDurableResultLifecycle
from src.integrations.mcp.temporary_results import (
    MCPDurableResultSnapshotAuthority,
)


class MCPDurableResultLifecycleError(RuntimeError):
    pass


class MCPDurableResultLifecycleManager:
    def __init__(
        self,
        storage: StoragePort,
        snapshot_authority: MCPDurableResultSnapshotAuthority,
        *,
        now_fn: Callable[[], datetime] | None = None,
        fault_hook: Callable[[str, MCPDurableResultLifecycle], None] | None = None,
    ) -> None:
        self._storage = storage
        self._snapshot_authority = snapshot_authority
        self._now = now_fn or (
            lambda: datetime.now(timezone.utc).replace(tzinfo=None)
        )
        self._fault_hook = fault_hook

    async def repair_incomplete(self, *, limit: int = 1000) -> int:
        rows = await self._storage.list_incomplete_mcp_durable_result_lifecycles(
            limit=limit
        )
        for row in rows:
            await self._delete(row)
        return len(rows)

    async def run_once(self, *, limit: int = 1000) -> tuple[int, int]:
        repaired = await self.repair_incomplete(limit=limit)
        deleting = await self._storage.claim_mcp_durable_result_deletions(
            self._now(), limit=limit
        )
        for row in deleting:
            await self._delete(row)
        return repaired, len(deleting)

    async def _delete(self, row: MCPDurableResultLifecycle) -> None:
        deleted = await self._snapshot_authority.delete_lifecycle_files(
            row,
            fault_hook=self._fault_hook,
        )
        if not deleted:
            released = await self._storage.release_mcp_durable_result_deletion(
                row.result_ref,
                row.revision,
                self._now() + timedelta(minutes=5),
            )
            if released is None:
                raise MCPDurableResultLifecycleError(
                    "mcp_durable_result_release_cas_conflict"
                )
            return
        saved = await self._storage.finish_mcp_durable_result_deletion(
            row.result_ref,
            row.revision,
            self._now(),
        )
        if saved is None:
            raise MCPDurableResultLifecycleError(
                "mcp_durable_result_delete_cas_conflict"
            )


__all__ = [
    "MCPDurableResultLifecycleError",
    "MCPDurableResultLifecycleManager",
]

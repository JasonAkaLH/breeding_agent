from __future__ import annotations

from collections.abc import Iterable

from src.orchestration.models import ExecutionInstance, InstanceState


def build_local_mcp_tool_instance(
    capability_ids: Iterable[str],
    *,
    instance_id: str = "inst-mcp-tool-local",
) -> ExecutionInstance:
    return ExecutionInstance(
        instance_id=instance_id,
        supported_capabilities=tuple(capability_ids),
        state=InstanceState.ONLINE,
        load_score=0,
    )

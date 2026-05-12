from __future__ import annotations

from collections.abc import Iterable

from src.orchestration.models import ExecutionInstance, InstanceState


def build_local_skill_executor_instance(
    capability_ids: Iterable[str],
    *,
    instance_id: str = "inst-skill-local",
) -> ExecutionInstance:
    return ExecutionInstance(
        instance_id=instance_id,
        supported_capabilities=tuple(capability_ids),
        state=InstanceState.ONLINE,
        load_score=0,
    )

from __future__ import annotations

from src.core.contracts import CapabilityExecutionRequest
from src.integrations.mysql_readonly import ReadonlyQueryResult


def make_request(
    capability_id: str,
    *,
    input_payload: dict | None = None,
    dependency_outputs: dict | None = None,
    metadata: dict | None = None,
) -> CapabilityExecutionRequest:
    return CapabilityExecutionRequest(
        capability_id=capability_id,
        conversation_id="conv-1",
        task_id="task-1",
        node_id=f"{capability_id}:node",
        input_payload=input_payload or {},
        dependency_outputs=dependency_outputs or {},
        metadata=metadata or {},
    )


def fake_query_result(*, columns: tuple[str, ...] = ("variety_name",), rows: tuple[dict, ...] = ({"variety_name": "先玉335"},)) -> ReadonlyQueryResult:
    return ReadonlyQueryResult(columns=columns, rows=rows, row_count=len(rows))

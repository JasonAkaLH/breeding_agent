from __future__ import annotations

import _bootstrap  # noqa: F401
from src.core.contracts import CapabilityExecutionRequest
from src.integrations.mysql_readonly import ReadonlyQueryResult
from tests.api.support import APITestCase
from tests.e2e.support import E2EAPITestCase

from _bootstrap import SKILL_ROOT


class _SQLQueryRuntimeMixin:
    def default_skill_roots(self):
        return (SKILL_ROOT.parent,)

    async def submit_message(
        self,
        *,
        conversation_id: str = "conv-1",
        content: str = "查询某个品种的基因型信息",
        capability_id: str | None = "skill.sql_query",
        metadata: dict | None = None,
    ):
        return await super().submit_message(
            conversation_id=conversation_id,
            content=content,
            capability_id=capability_id,
            metadata=metadata,
        )


class SQLQueryAPITestCase(_SQLQueryRuntimeMixin, APITestCase):
    pass


class SQLQueryE2EAPITestCase(_SQLQueryRuntimeMixin, E2EAPITestCase):
    pass


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


def fake_query_result(*, columns: tuple[str, ...] = ("variety_name",), rows: tuple[dict, ...] = ({"variety_name": "龙粳33"},)) -> ReadonlyQueryResult:
    return ReadonlyQueryResult(columns=columns, rows=rows, row_count=len(rows))

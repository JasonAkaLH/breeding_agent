from __future__ import annotations

from src.core.contracts import CapabilityContract, CapabilityExecutionRequest, CapabilityExecutionResult, ExecutorPort
from src.integrations.mysql_readonly import MySQLReadonlyAdapter
from src.orchestration.models import ExecutionInstance, InstanceState

from .intent_route import SQLQueryIntentRouteCapability
from .result_filtering import SQLQueryResultFilteringCapability
from .schema_context_prepare import SQLQuerySchemaContextPrepareCapability
from .sql_execute_readonly import SQLQuerySQLExecuteReadonlyCapability
from .sql_generate import SQLQuerySQLGenerateCapability
from .sql_guard import SQLQuerySQLGuardCapability
from .workflow import SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS


class SQLQueryExecutor(ExecutorPort):
    def __init__(
        self,
        *,
        sql_generator=None,
        llm_text_generator=None,
        mysql_adapter: MySQLReadonlyAdapter | None = None,
        trim_max_tokens: int | None = None,
    ) -> None:
        self._capabilities: dict[str, CapabilityContract] = {
            "sql_query.intent_route": SQLQueryIntentRouteCapability(),
            "sql_query.schema_context_prepare": SQLQuerySchemaContextPrepareCapability(),
            "sql_query.sql_generate": SQLQuerySQLGenerateCapability(
                generator=sql_generator,
                llm_text_generator=llm_text_generator,
            ),
            "sql_query.sql_guard": SQLQuerySQLGuardCapability(),
            "sql_query.sql_execute_readonly": SQLQuerySQLExecuteReadonlyCapability(adapter=mysql_adapter),
            "sql_query.result_filtering": SQLQueryResultFilteringCapability(
                llm_text_generator=llm_text_generator,
                trim_max_tokens=trim_max_tokens,
            ),
        }

    def supports(self, capability_id: str) -> bool:
        return capability_id in self._capabilities

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        capability = self._capabilities.get(request.capability_id)
        if capability is None:
            raise ValueError(f"Unsupported SQLQuery capability_id: {request.capability_id}")
        return await capability.execute(request)

    @property
    def supported_capabilities(self) -> tuple[str, ...]:
        return tuple(self._capabilities.keys())


def build_local_sql_query_instance(*, instance_id: str = "inst-sql-query-local") -> ExecutionInstance:
    return ExecutionInstance(
        instance_id=instance_id,
        supported_capabilities=tuple(descriptor.capability_id for descriptor in SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS),
        state=InstanceState.ONLINE,
        load_score=0,
    )

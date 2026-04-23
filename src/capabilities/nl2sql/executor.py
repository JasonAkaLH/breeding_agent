from __future__ import annotations

from src.core.contracts import CapabilityContract, CapabilityExecutionRequest, CapabilityExecutionResult, ExecutorPort
from src.integrations.mysql_readonly import MySQLReadonlyAdapter
from src.orchestration.models import ExecutionInstance, InstanceState

from .intent_route import NL2SQLIntentRouteCapability
from .result_summarize import NL2SQLResultSummarizeCapability
from .schema_context_prepare import NL2SQLSchemaContextPrepareCapability
from .sql_execute_readonly import NL2SQLSQLExecuteReadonlyCapability
from .sql_generate import NL2SQLSQLGenerateCapability
from .sql_guard import NL2SQLSQLGuardCapability
from .workflow import NL2SQL_CAPABILITY_DESCRIPTORS


class NL2SQLExecutor(ExecutorPort):
    def __init__(
        self,
        *,
        sql_generator=None,
        summarizer=None,
        mysql_adapter: MySQLReadonlyAdapter | None = None,
    ) -> None:
        self._capabilities: dict[str, CapabilityContract] = {
            "nl2sql.intent_route": NL2SQLIntentRouteCapability(),
            "nl2sql.schema_context_prepare": NL2SQLSchemaContextPrepareCapability(),
            "nl2sql.sql_generate": NL2SQLSQLGenerateCapability(generator=sql_generator),
            "nl2sql.sql_guard": NL2SQLSQLGuardCapability(),
            "nl2sql.sql_execute_readonly": NL2SQLSQLExecuteReadonlyCapability(adapter=mysql_adapter),
            "nl2sql.result_summarize": NL2SQLResultSummarizeCapability(summarizer=summarizer),
        }

    def supports(self, capability_id: str) -> bool:
        return capability_id in self._capabilities

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        capability = self._capabilities.get(request.capability_id)
        if capability is None:
            raise ValueError(f"Unsupported NL2SQL capability_id: {request.capability_id}")
        return await capability.execute(request)

    @property
    def supported_capabilities(self) -> tuple[str, ...]:
        return tuple(self._capabilities.keys())


def build_local_nl2sql_instance(*, instance_id: str = "inst-nl2sql-local") -> ExecutionInstance:
    return ExecutionInstance(
        instance_id=instance_id,
        supported_capabilities=tuple(descriptor.capability_id for descriptor in NL2SQL_CAPABILITY_DESCRIPTORS),
        state=InstanceState.ONLINE,
        load_score=0,
    )

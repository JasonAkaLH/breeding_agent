from typing import Annotated

from fastapi import APIRouter, Depends

from multi_agent_framework.api.dependencies import get_coordinator, get_registry, get_settings
from multi_agent_framework.config import Settings
from multi_agent_framework.core.contracts import AgentDescriptor, AgentRequest, AgentResponse
from multi_agent_framework.core.registry import AgentRegistry
from multi_agent_framework.orchestration.coordinator import Coordinator

router = APIRouter()
SettingsDependency = Annotated[Settings, Depends(get_settings)]
RegistryDependency = Annotated[AgentRegistry, Depends(get_registry)]
CoordinatorDependency = Annotated[Coordinator, Depends(get_coordinator)]


@router.get("/healthz")
async def healthz(settings: SettingsDependency) -> dict[str, str | bool]:
    return {
        "status": "ok",
        "service": settings.service_name,
        "environment": settings.app_env,
        "debug": settings.debug,
    }


@router.get("/agents", response_model=list[AgentDescriptor])
async def list_agents(registry: RegistryDependency) -> list[AgentDescriptor]:
    return registry.list_agents()


@router.post("/workflows/execute", response_model=AgentResponse)
async def execute_workflow(
    payload: AgentRequest,
    coordinator: CoordinatorDependency,
) -> AgentResponse:
    return await coordinator.execute(payload)

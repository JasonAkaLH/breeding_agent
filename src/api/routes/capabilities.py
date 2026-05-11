from __future__ import annotations

from fastapi import APIRouter, Request

from ..dto import CapabilityListResponse, CapabilityResponse
from ..runtime import ApiRuntime

router = APIRouter()


def _runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


@router.get("/api/v1/capabilities", response_model=CapabilityListResponse)
async def list_capabilities(request: Request) -> CapabilityListResponse:
    runtime = _runtime(request)
    return CapabilityListResponse(
        capabilities=[
            CapabilityResponse(
                capability_id=descriptor.capability_id,
                name=descriptor.name,
                description=descriptor.description,
                version=descriptor.version,
                status="active" if descriptor.enabled else "disabled",
                kind=descriptor.kind,
                source=descriptor.source,
            )
            for descriptor in runtime.capability_registry.list(public_only=True)
        ]
    )

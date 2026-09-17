from __future__ import annotations

from fastapi import APIRouter, Request

from ..dto import CapabilityListResponse, CapabilityResponse
from ..runtime_access import runtime_from_request as _runtime

router = APIRouter()


@router.get("/api/v1/capabilities", response_model=CapabilityListResponse)
async def list_capabilities(request: Request) -> CapabilityListResponse:
    runtime = _runtime(request)
    await runtime.refresh_skills_for_capabilities_list()
    return CapabilityListResponse(
        capabilities=[
            CapabilityResponse(
                capability_id=descriptor.capability_id,
                name=descriptor.name,
                display_name=descriptor.display_name,
                description=descriptor.description,
                version=descriptor.version,
                status="active" if descriptor.enabled else "disabled",
                kind=descriptor.kind,
                source=descriptor.source,
                source_path=descriptor.source_path,
            )
            for descriptor in runtime.capability_registry.list(public_only=True)
        ]
    )

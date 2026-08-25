from __future__ import annotations

from fastapi import APIRouter, Request

from ..auth import require_authenticated_user
from ..dto import ModelEditionOptionResponse, ModelEditionsResponse
from ..runtime_access import runtime_from_request as _runtime

router = APIRouter()


@router.get("/api/v1/config/model-editions", response_model=ModelEditionsResponse)
async def get_model_editions(request: Request) -> ModelEditionsResponse:
    await require_authenticated_user(request)
    payload = _runtime(request).model_editions_payload()
    return ModelEditionsResponse(
        default_model_edition=payload["default_model_edition"],
        options=[ModelEditionOptionResponse(**option) for option in payload["options"]],
    )

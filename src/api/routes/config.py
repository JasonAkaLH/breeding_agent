from __future__ import annotations

from fastapi import APIRouter, Request

from ..auth import require_authenticated_user
from ..dto import ModelEditionOptionResponse, ModelEditionsResponse
from ..runtime import ApiRuntime

router = APIRouter()


def _runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


@router.get("/api/v1/config/model-editions", response_model=ModelEditionsResponse)
async def get_model_editions(request: Request) -> ModelEditionsResponse:
    await require_authenticated_user(request)
    payload = _runtime(request).model_editions_payload()
    return ModelEditionsResponse(
        default_model_edition=payload["default_model_edition"],
        options=[ModelEditionOptionResponse(**option) for option in payload["options"]],
    )

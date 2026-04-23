from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from src.lifecycle.errors import ConversationBusyError

from ..dto import MessageAcceptedResponse, SubmitMessageRequest
from ..runtime import ApiRuntime

router = APIRouter()


def _runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


@router.post(
    "/api/v1/conversations/{conversation_id}/messages",
    response_model=MessageAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_message(conversation_id: str, body: SubmitMessageRequest, request: Request) -> MessageAcceptedResponse:
    runtime = _runtime(request)
    try:
        message, task = await runtime.submit_message(conversation_id, body)
    except ConversationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return MessageAcceptedResponse(
        conversation_id=conversation_id,
        message_id=message.message_id,
        task_id=task.task_id,
        status="accepted",
    )

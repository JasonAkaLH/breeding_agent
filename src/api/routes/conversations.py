from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from src.core.models import Conversation
from src.lifecycle.errors import ConversationBusyError

from ..auth import require_authenticated_user, require_conversation_owner
from ..dto import (
    ConversationListResponse,
    ConversationMessagesResponse,
    ConversationSummaryResponse,
    DeleteConversationRequest,
    DeleteConversationResponse,
    MessageAcceptedResponse,
    MessageResponse,
    RenameConversationRequest,
    SubmitMessageRequest,
)
from ..runtime import ApiRuntime

router = APIRouter()


def _runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


def _conversation_summary_response(conversation: Conversation) -> ConversationSummaryResponse:
    return ConversationSummaryResponse(
        conversation_id=conversation.conversation_id,
        account_id=conversation.account_id,
        status=str(conversation.status),
        current_task_id=conversation.current_task_id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


@router.post(
    "/api/v1/conversations/chat-messages",
    response_model=MessageAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_message(body: SubmitMessageRequest, request: Request) -> MessageAcceptedResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request, required_scopes=("conversation:write",))
    conversation_id = body.conversation_id
    try:
        message, task = await runtime.submit_message(conversation_id, body, authenticated_account_id=user.username)
    except ConversationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return MessageAcceptedResponse(
        conversation_id=conversation_id,
        message_id=message.message_id,
        task_id=task.task_id,
        status="accepted",
    )


@router.get("/api/v1/conversations", response_model=ConversationListResponse)
async def list_conversations(request: Request) -> ConversationListResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request, required_scopes=("conversation:read",))
    conversations = await runtime.storage.list_conversations_for_account(user.username)
    return ConversationListResponse(
        conversations=[_conversation_summary_response(conversation) for conversation in conversations]
    )


@router.patch("/api/v1/conversations", response_model=ConversationSummaryResponse)
async def rename_conversation(body: RenameConversationRequest, request: Request) -> ConversationSummaryResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request, required_scopes=("conversation:write",))
    conversation_id = body.conversation_id
    try:
        conversation = await runtime.rename_conversation(conversation_id, body.title, account_id=user.username)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}") from exc
    except ValueError as exc:
        message = str(exc)
        status_code = status.HTTP_404_NOT_FOUND if message.startswith("Unknown conversation") else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=status_code, detail=message) from exc
    return _conversation_summary_response(conversation)


@router.get("/api/v1/conversations/{conversation_id}/messages", response_model=ConversationMessagesResponse)
async def list_conversation_messages(conversation_id: str, request: Request) -> ConversationMessagesResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request, required_scopes=("conversation:read",))
    await require_conversation_owner(runtime, conversation_id, user)
    await runtime.sync_assistant_history_messages(conversation_id)
    messages = await runtime.storage.list_messages_for_conversation(conversation_id)
    return ConversationMessagesResponse(
        conversation_id=conversation_id,
        messages=[
            MessageResponse(
                message_id=message.message_id,
                conversation_id=message.conversation_id,
                role=str(message.role),
                content=message.content,
                task_id=message.task_id,
                stream_status=message.stream_status,
                created_at=message.created_at,
            )
            for message in messages
        ],
    )


@router.delete("/api/v1/conversations", response_model=DeleteConversationResponse)
async def delete_conversation(body: DeleteConversationRequest, request: Request) -> DeleteConversationResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request, required_scopes=("conversation:write",))
    conversation_id = body.conversation_id
    await require_conversation_owner(runtime, conversation_id, user)
    try:
        result = await runtime.delete_conversation(conversation_id, account_id=user.username)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}") from exc
    return DeleteConversationResponse(
        conversation_id=str(result["conversation_id"]),
        deleted=bool(result["deleted"]),
        cancelled_task_ids=list(result["cancelled_task_ids"]),
        deleted_counts=dict(result["deleted_counts"]),
    )

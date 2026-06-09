from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from src.core.enums import ConversationStatus, MessageRole
from src.core.models import Artifact, Conversation, Message
from src.lifecycle.errors import ConversationBusyError
from src.api.upload_store import UploadValidationError

from ..artifact_responses import artifact_response, should_return_history_display_artifact
from ..auth import require_authenticated_user, require_conversation_owner
from ..dto import (
    ConversationListResponse,
    ConversationMessagesResponse,
    ConversationSummaryResponse,
    DeleteConversationRequest,
    DeleteConversationResponse,
    ArtifactResponse,
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
        username=conversation.username,
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
    user = await require_authenticated_user(request)
    conversation_id = body.conversation_id
    try:
        result = await runtime.submit_chat_message(conversation_id, body, authenticated_username=user.username)
    except ConversationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}") from exc
    except UploadValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return MessageAcceptedResponse(**result)


@router.get("/api/v1/conversations", response_model=ConversationListResponse)
async def list_conversations(request: Request) -> ConversationListResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    conversations = await runtime.storage.list_conversations_for_username(user.username)
    return ConversationListResponse(
        conversations=[_conversation_summary_response(conversation) for conversation in conversations]
    )


@router.patch("/api/v1/conversations", response_model=ConversationSummaryResponse)
async def rename_conversation(body: RenameConversationRequest, request: Request) -> ConversationSummaryResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    conversation_id = body.conversation_id
    try:
        conversation = await runtime.rename_conversation(conversation_id, body.title, username=user.username)
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
    user = await require_authenticated_user(request)
    await require_conversation_owner(runtime, conversation_id, user)
    await runtime.sync_assistant_history_messages(conversation_id)
    messages = await runtime.storage.list_messages_for_conversation(conversation_id)
    artifacts_by_task_id = await _history_display_artifacts_by_task_id(runtime, conversation_id, messages)
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
                artifacts=(
                    artifacts_by_task_id.get(message.task_id, [])
                    if message.role == MessageRole.ASSISTANT and message.task_id is not None
                    else []
                ),
            )
            for message in messages
        ],
    )


async def _history_display_artifacts_by_task_id(
    runtime: ApiRuntime,
    conversation_id: str,
    messages: list[Message],
) -> dict[str, list[ArtifactResponse]]:
    assistant_task_ids = {
        message.task_id
        for message in messages
        if message.role == MessageRole.ASSISTANT and message.task_id is not None
    }
    if not assistant_task_ids:
        return {}

    grouped: dict[str, list] = {task_id: [] for task_id in assistant_task_ids}
    artifacts = await runtime.storage.list_artifacts_for_conversation(conversation_id)
    for artifact in artifacts:
        if _is_history_artifact_for_assistant_message(artifact, assistant_task_ids):
            grouped[artifact.task_id].append(artifact_response(artifact))
    return grouped


def _is_history_artifact_for_assistant_message(artifact: Artifact, assistant_task_ids: set[str]) -> bool:
    return artifact.task_id in assistant_task_ids and should_return_history_display_artifact(artifact)


@router.delete("/api/v1/conversations", response_model=DeleteConversationResponse)
async def delete_conversation(body: DeleteConversationRequest, request: Request) -> DeleteConversationResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    conversation_id = body.conversation_id
    conversation = await runtime.storage.get_conversation(conversation_id)
    if (
        conversation is None
        or conversation.username != user.username
        or conversation.status not in {ConversationStatus.ACTIVE, ConversationStatus.DELETING}
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}")
    try:
        result = await runtime.delete_conversation(conversation_id, username=user.username)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}") from exc
    return DeleteConversationResponse(
        conversation_id=str(result["conversation_id"]),
        deleted=bool(result["deleted"]),
        cancelled_task_ids=list(result["cancelled_task_ids"]),
        deleted_counts=dict(result["deleted_counts"]),
        delete_status=str(result.get("delete_status") or "completed"),
        runner_id=result.get("runner_id"),
        started_at=result.get("started_at"),
        finished_at=result.get("finished_at"),
        error_code=result.get("error_code"),
    )

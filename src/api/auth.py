from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from src.api.runtime import ApiRuntime
from src.auth import AuthTokenValidationError
from src.core.models import Conversation, Task


@dataclass(slots=True, frozen=True)
class AuthenticatedUser:
    username: str


def _runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


def bearer_token_from_request(request: Request) -> str | None:
    value = request.headers.get("Authorization", "").strip()
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def require_authenticated_user(request: Request) -> AuthenticatedUser:
    token = bearer_token_from_request(request)
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        username_token = await _runtime(request).get_username_for_bearer(token)
    except AuthTokenValidationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication expired") from exc
    return AuthenticatedUser(username=username_token.username)


async def require_current_bearer_for_user(request: Request, username: str) -> None:
    token = bearer_token_from_request(request)
    if token is None or not await _runtime(request).bearer_token_is_current_for_username(token, username):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication expired")


async def require_conversation_owner(runtime: ApiRuntime, conversation_id: str, user: AuthenticatedUser) -> Conversation:
    conversation = await runtime.storage.get_conversation(conversation_id)
    if conversation is None or conversation.username != user.username:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}")
    return conversation


async def get_optional_owned_conversation(runtime: ApiRuntime, conversation_id: str, user: AuthenticatedUser) -> Conversation | None:
    conversation = await runtime.storage.get_conversation(conversation_id)
    if conversation is not None and conversation.username != user.username:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}")
    return conversation


async def require_task_owner(runtime: ApiRuntime, task_id: str, user: AuthenticatedUser) -> Task:
    task = await runtime.storage.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown task: {task_id}")
    conversation = await runtime.storage.get_conversation(task.conversation_id)
    if conversation is None or conversation.username != user.username:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown task: {task_id}")
    return task

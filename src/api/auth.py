from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response, status

from src.api.runtime import ApiRuntime
from src.core.models import Conversation, Task

SESSION_COOKIE_NAME = "maf_session"
SESSION_COOKIE_MAX_AGE_SECONDS = 28_800


@dataclass(slots=True, frozen=True)
class AuthenticatedUser:
    username: str


def _runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


async def require_authenticated_user(request: Request) -> AuthenticatedUser:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    user = await _runtime(request).get_session_user(session_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return AuthenticatedUser(username=user.username)


async def require_conversation_owner(runtime: ApiRuntime, conversation_id: str, user: AuthenticatedUser) -> Conversation:
    conversation = await runtime.storage.get_conversation(conversation_id)
    if conversation is None or conversation.account_id != user.username:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}")
    return conversation


async def get_optional_owned_conversation(runtime: ApiRuntime, conversation_id: str, user: AuthenticatedUser) -> Conversation | None:
    conversation = await runtime.storage.get_conversation(conversation_id)
    if conversation is not None and conversation.account_id != user.username:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}")
    return conversation


async def require_task_owner(runtime: ApiRuntime, task_id: str, user: AuthenticatedUser) -> Task:
    task = await runtime.storage.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown task: {task_id}")
    conversation = await runtime.storage.get_conversation(task.conversation_id)
    if conversation is None or conversation.account_id != user.username:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown task: {task_id}")
    return task


def should_use_secure_cookie(request: Request) -> bool:
    configured = os.getenv("MAF_AUTH_COOKIE_SECURE", "").strip().lower()
    if configured in {"1", "true", "yes", "on"}:
        return True
    if configured in {"0", "false", "no", "off"}:
        return False
    return request.url.scheme == "https"


def set_session_cookie(response: Response, session_id: str, *, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")

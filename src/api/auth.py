from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, Request, Response, status

from src.auth import AuthTokenScopeError
from src.api.runtime import ApiRuntime
from src.core.models import Conversation, Task

SESSION_COOKIE_NAME = "__Host-maf_session"
LEGACY_SESSION_COOKIE_NAME = "maf_session"
SESSION_COOKIE_MAX_AGE_SECONDS = 28_800

AuthSource = Literal["cookie", "bearer"]


@dataclass(slots=True, frozen=True)
class AuthenticatedUser:
    username: str
    auth_source: AuthSource = "cookie"
    scopes: frozenset[str] = frozenset()


@dataclass(slots=True, frozen=True)
class ResolvedSessionCookie:
    session_id: str
    cookie_name: str


def _runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


def resolve_session_cookie(request: Request) -> ResolvedSessionCookie | None:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        return ResolvedSessionCookie(session_id=session_id, cookie_name=SESSION_COOKIE_NAME)
    legacy_session_id = request.cookies.get(LEGACY_SESSION_COOKIE_NAME)
    if legacy_session_id:
        return ResolvedSessionCookie(session_id=legacy_session_id, cookie_name=LEGACY_SESSION_COOKIE_NAME)
    return None


def _bearer_token(request: Request) -> str | None:
    value = request.headers.get("Authorization", "").strip()
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip()


async def require_authenticated_user(
    request: Request,
    required_scopes: tuple[str, ...] = (),
    *,
    require_cookie_session: bool = False,
) -> AuthenticatedUser:
    runtime = _runtime(request)
    if not require_cookie_session:
        token = _bearer_token(request)
        if token is not None:
            try:
                result = await runtime.get_bearer_token_user(token, required_scopes=required_scopes)
            except AuthTokenScopeError as exc:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"code": "insufficient_scope", "missing_scopes": list(exc.missing_scopes)},
                ) from exc
            if result is None:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
            user, api_token = result
            return AuthenticatedUser(username=user.username, auth_source="bearer", scopes=frozenset(api_token.scopes))

    resolved = resolve_session_cookie(request)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    user = await runtime.get_session_user(resolved.session_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return AuthenticatedUser(username=user.username, auth_source="cookie")


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


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=True,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(LEGACY_SESSION_COOKIE_NAME, path="/")

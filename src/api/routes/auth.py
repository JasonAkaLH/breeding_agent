from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from src.auth import AuthTokenValidationError, AuthValidationError

from ..auth import bearer_token_from_request, require_authenticated_user
from ..dto import AuthTokenResponse, AuthUserResponse, LoginRequest, LogoutResponse, UserResponse
from ..runtime import ApiRuntime

router = APIRouter()


def _runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


@router.post("/api/v1/auth/login", response_model=AuthTokenResponse)
async def login(body: LoginRequest, request: Request) -> AuthTokenResponse:
    try:
        token_record, access_token = await _runtime(request).login_username(body.username)
    except AuthValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return AuthTokenResponse(user=UserResponse(username=token_record.username), access_token=access_token)


@router.get("/api/v1/auth/me", response_model=AuthUserResponse)
async def me(request: Request) -> AuthUserResponse:
    user = await require_authenticated_user(request)
    return AuthUserResponse(user=UserResponse(username=user.username))


@router.post("/api/v1/auth/logout", response_model=LogoutResponse)
async def logout(request: Request) -> LogoutResponse:
    token = bearer_token_from_request(request)
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        await _runtime(request).logout_bearer(token)
    except AuthTokenValidationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication expired") from exc
    return LogoutResponse(logged_out=True)


@router.post("/api/v1/auth/refresh-token", response_model=AuthTokenResponse)
async def refresh_token(request: Request) -> AuthTokenResponse:
    token = bearer_token_from_request(request)
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        token_record, access_token = await _runtime(request).refresh_bearer(token)
    except AuthTokenValidationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication expired") from exc
    return AuthTokenResponse(user=UserResponse(username=token_record.username), access_token=access_token)

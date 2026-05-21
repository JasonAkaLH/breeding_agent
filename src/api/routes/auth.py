from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from src.auth import AuthTokenValidationError, AuthValidationError, DuplicateUsernameError
from src.core.models import AuthApiToken

from ..auth import (
    clear_session_cookie,
    require_authenticated_user,
    resolve_session_cookie,
    set_session_cookie,
)
from ..dto import (
    ApiTokenListResponse,
    ApiTokenResponse,
    AuthUserResponse,
    CaptchaChallengeResponse,
    CreateApiTokenRequest,
    CreateApiTokenResponse,
    LoginRequest,
    LogoutResponse,
    RegisterRequest,
    RevokeApiTokenRequest,
    RevokeApiTokenResponse,
    UserResponse,
)
from ..runtime import ApiRuntime

router = APIRouter()


def _runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


@router.post("/api/v1/auth/captcha", response_model=CaptchaChallengeResponse)
async def create_captcha(request: Request) -> CaptchaChallengeResponse:
    runtime = _runtime(request)
    challenge, _code, image_svg = await runtime.create_captcha_challenge()
    return CaptchaChallengeResponse(
        captcha_id=challenge.captcha_id,
        image_svg=image_svg,
        expires_in_seconds=max(0, int((challenge.expires_at - runtime._utcnow_naive()).total_seconds())),
    )


@router.post("/api/v1/auth/login", response_model=AuthUserResponse)
async def login(body: LoginRequest, request: Request, response: Response) -> AuthUserResponse:
    session = await _runtime(request).login(
        body.username,
        body.password,
        body.captcha_id,
        body.captcha_code,
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials or verification code",
        )
    set_session_cookie(response, session.session_id)
    return AuthUserResponse(user=UserResponse(username=session.username))


@router.post("/api/v1/auth/register", response_model=AuthUserResponse)
async def register(body: RegisterRequest, request: Request, response: Response) -> AuthUserResponse:
    try:
        session = await _runtime(request).register_user(
            body.username,
            body.password,
            body.captcha_id,
            body.captcha_code,
        )
    except AuthValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except DuplicateUsernameError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists") from exc
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid verification code")
    set_session_cookie(response, session.session_id)
    return AuthUserResponse(user=UserResponse(username=session.username))


@router.get("/api/v1/auth/me", response_model=AuthUserResponse)
async def me(request: Request) -> AuthUserResponse:
    user = await require_authenticated_user(request)
    return AuthUserResponse(user=UserResponse(username=user.username))


@router.post("/api/v1/auth/logout", response_model=LogoutResponse)
async def logout(request: Request, response: Response) -> LogoutResponse:
    resolved = resolve_session_cookie(request)
    if resolved is not None:
        await _runtime(request).revoke_session(resolved.session_id)
    clear_session_cookie(response)
    return LogoutResponse(logged_out=True)


def _api_token_response(token: AuthApiToken) -> ApiTokenResponse:
    return ApiTokenResponse(
        token_id=token.token_id,
        client_name=token.client_name,
        scopes=list(token.scopes),
        expires_at=token.expires_at,
        revoked_at=token.revoked_at,
        created_at=token.created_at,
        last_used_at=token.last_used_at,
    )


@router.post("/api/v1/auth/api-tokens", response_model=CreateApiTokenResponse, status_code=status.HTTP_201_CREATED)
async def create_api_token(body: CreateApiTokenRequest, request: Request) -> CreateApiTokenResponse:
    user = await require_authenticated_user(request, require_cookie_session=True)
    try:
        token, access_token = await _runtime(request).create_api_token(
            username=user.username,
            client_name=body.client_name,
            scopes=tuple(body.scopes),
            ttl_seconds=body.ttl_seconds,
        )
    except AuthTokenValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": exc.code, "message": str(exc)}) from exc
    response = _api_token_response(token)
    return CreateApiTokenResponse(**response.model_dump(), access_token=access_token)


@router.get("/api/v1/auth/api-tokens", response_model=ApiTokenListResponse)
async def list_api_tokens(request: Request) -> ApiTokenListResponse:
    user = await require_authenticated_user(request, require_cookie_session=True)
    tokens = await _runtime(request).list_api_tokens_for_user(user.username)
    return ApiTokenListResponse(tokens=[_api_token_response(token) for token in tokens])


@router.delete("/api/v1/auth/api-tokens", response_model=RevokeApiTokenResponse)
async def revoke_api_token(body: RevokeApiTokenRequest, request: Request) -> RevokeApiTokenResponse:
    user = await require_authenticated_user(request, require_cookie_session=True)
    token = await _runtime(request).revoke_api_token(username=user.username, token_id=body.token_id)
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown API token: {body.token_id}")
    return RevokeApiTokenResponse(token_id=token.token_id, revoked=token.revoked_at is not None)

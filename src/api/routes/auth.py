from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from src.auth import AuthValidationError, DuplicateUsernameError

from ..auth import (
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    require_authenticated_user,
    set_session_cookie,
    should_use_secure_cookie,
)
from ..dto import (
    AuthUserResponse,
    CaptchaChallengeResponse,
    LoginRequest,
    LogoutResponse,
    RegisterRequest,
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
    set_session_cookie(response, session.session_id, secure=should_use_secure_cookie(request))
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
    set_session_cookie(response, session.session_id, secure=should_use_secure_cookie(request))
    return AuthUserResponse(user=UserResponse(username=session.username))


@router.get("/api/v1/auth/me", response_model=AuthUserResponse)
async def me(request: Request) -> AuthUserResponse:
    user = await require_authenticated_user(request)
    return AuthUserResponse(user=UserResponse(username=user.username))


@router.post("/api/v1/auth/logout", response_model=LogoutResponse)
async def logout(request: Request, response: Response) -> LogoutResponse:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        await _runtime(request).revoke_session(session_id)
    clear_session_cookie(response)
    return LogoutResponse(logged_out=True)

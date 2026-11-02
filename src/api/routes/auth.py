from __future__ import annotations

import json
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from src.auth import AuthTokenValidationError, AuthValidationError

from ..auth import bearer_token_from_request, require_authenticated_user
from ..dto import AuthTokenResponse, AuthUserResponse, LoginRequest, LogoutResponse, UserResponse
from ..runtime_access import runtime_from_request as _runtime

router = APIRouter()

_LOGIN_EMBED_ORIGINS_PATH = Path(__file__).resolve().parents[3] / "login_embed_origins.yaml"


def _login_frame_ancestors() -> str:
    try:
        config = yaml.safe_load(_LOGIN_EMBED_ORIGINS_PATH.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or set(config) != {"allowed_origins"}:
            raise ValueError("Expected an allowed_origins mapping")
        if not isinstance(config["allowed_origins"], list):
            raise ValueError("Expected an origin list")
        origins: list[str] = []
        for value in config["allowed_origins"]:
            if not isinstance(value, str):
                raise ValueError("Expected a string origin")
            origin = value.strip()
            parsed = urlsplit(origin)
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
                and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 or char in "*;'\"\\<>" for char in origin)
            )
            _ = parsed.port
            if not valid:
                raise ValueError("Invalid origin")
            origins.append(f"{parsed.scheme}://{parsed.netloc}")
        return " ".join(dict.fromkeys(origins)) or "'none'"
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Invalid login embed origin configuration",
        ) from exc


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


@router.get("/api/v1/auth/login-redirect", response_class=HTMLResponse)
async def login_redirect(username: str, request: Request) -> HTMLResponse:
    """Open through the frontend origin to log in and navigate to its chat page."""
    frame_ancestors = _login_frame_ancestors()
    result = await login(LoginRequest(username=username), request)
    nonce = secrets.token_urlsafe(16)
    token_json = json.dumps(result.access_token).replace("<", "\\u003c")
    return HTMLResponse(
        content=f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>登录育种助手</title></head>
<body>
<p id="login-status" role="status">正在进入对话界面…</p>
<noscript>请启用 JavaScript 后重新打开登录入口。</noscript>
<script nonce="{nonce}">
try {{
  window.localStorage.setItem('maf.frontend.access_token', {token_json});
  // Resolve against the browser URL so reverse-proxy subpaths are preserved.
  window.location.replace(new URL('../../../', window.location.href).href);
}} catch {{
  document.getElementById('login-status').textContent =
    '无法保存登录状态，请允许此网站使用浏览器存储后重试。';
}}
</script>
</body>
</html>""",
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; "
                f"base-uri 'none'; frame-ancestors {frame_ancestors}"
            ),
        },
    )


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

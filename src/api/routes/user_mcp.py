from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from src.integrations.mcp.credentials import CredentialSecurityError
from src.integrations.mcp.endpoint_policy import EndpointPolicyError
from src.integrations.mcp.headers import HeaderPolicyError
from src.integrations.mcp.user_config import UserMCPConfigError, UserMCPConfigService

from ..auth import require_authenticated_user
from ..dto import (
    CreateUserMCPServerRequest,
    PatchUserMCPServerRequest,
    UserMCPDeletePendingResponse,
    UserMCPServerListResponse,
    UserMCPServerResponse,
)


router = APIRouter(prefix="/api/v1/mcp/servers", tags=["user-mcp"])


def _service(request: Request) -> UserMCPConfigService:
    service = getattr(request.app.state.runtime, "user_mcp_config_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "mcp_feature_unavailable"},
        )
    return service


def _response(server) -> UserMCPServerResponse:
    return UserMCPServerResponse(
        server_id=server.server_id,
        display_name=server.display_name,
        routing_description=server.routing_description,
        endpoint_url=server.endpoint_url,
        transport=str(server.transport),
        protocol_preference=str(server.protocol_preference),
        auth_type=str(server.auth_type),
        auth_metadata=dict(server.auth_metadata),
        enabled=server.enabled,
        health_status=str(server.health_status),
        credential_configured=server.credential_configured,
        config_version=server.config_version,
        security_version=server.security_version,
        last_tested_at=server.last_tested_at,
        last_test_error_code=server.last_test_error_code,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


def _raise_safe_error(exc: Exception) -> None:
    if isinstance(exc, UserMCPConfigError):
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code},
        ) from exc
    if isinstance(exc, (EndpointPolicyError, HeaderPolicyError)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": exc.code},
        ) from exc
    if isinstance(exc, CredentialSecurityError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code},
        ) from exc
    raise exc


@router.get("", response_model=UserMCPServerListResponse)
async def list_user_mcp_servers(request: Request) -> UserMCPServerListResponse:
    user = await require_authenticated_user(request)
    servers = await _service(request).list_servers(user.username)
    return UserMCPServerListResponse(servers=[_response(server) for server in servers])


@router.post("", response_model=UserMCPServerResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_user_mcp_server(
    payload: CreateUserMCPServerRequest, request: Request
) -> UserMCPServerResponse:
    user = await require_authenticated_user(request)
    try:
        server = await _service(request).create_server(user.username, payload.model_dump())
    except Exception as exc:
        _raise_safe_error(exc)
    return _response(server)


@router.get("/{server_id}", response_model=UserMCPServerResponse)
async def get_user_mcp_server(server_id: str, request: Request) -> UserMCPServerResponse:
    user = await require_authenticated_user(request)
    try:
        server = await _service(request).get_server(user.username, server_id)
    except Exception as exc:
        _raise_safe_error(exc)
    return _response(server)


@router.patch("/{server_id}", response_model=UserMCPServerResponse)
async def patch_user_mcp_server(
    server_id: str, payload: PatchUserMCPServerRequest, request: Request
) -> UserMCPServerResponse:
    user = await require_authenticated_user(request)
    try:
        server = await _service(request).patch_server(
            user.username, server_id, payload.model_dump(exclude_unset=True)
        )
    except Exception as exc:
        _raise_safe_error(exc)
    return _response(server)


@router.post("/{server_id}/test", response_model=UserMCPServerResponse, status_code=status.HTTP_202_ACCEPTED)
async def test_user_mcp_server(server_id: str, request: Request) -> UserMCPServerResponse:
    user = await require_authenticated_user(request)
    try:
        server = await _service(request).test_server(user.username, server_id)
    except Exception as exc:
        _raise_safe_error(exc)
    return _response(server)


@router.delete(
    "/{server_id}",
    response_model=None,
    responses={
        status.HTTP_202_ACCEPTED: {"model": UserMCPDeletePendingResponse},
        status.HTTP_204_NO_CONTENT: {"description": "Deleted"},
    },
)
async def delete_user_mcp_server(server_id: str, request: Request) -> Response | UserMCPDeletePendingResponse:
    user = await require_authenticated_user(request)
    try:
        deleted = await _service(request).delete_server(user.username, server_id)
    except Exception as exc:
        _raise_safe_error(exc)
    if deleted:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    pending = UserMCPDeletePendingResponse(server_id=server_id)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=pending.model_dump(),
    )

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from ..auth import require_authenticated_user
from ..dto import UserMCPToolGrantListResponse, UserMCPToolGrantResponse


router = APIRouter(prefix="/api/v1/mcp/grants", tags=["user-mcp"])


async def _audit_grant_revoked(request: Request, owner_user_id: str, grant_id: str) -> None:
    service = request.app.state.runtime.user_mcp_audit_service
    if service is None:
        return
    try:
        await service.record(
            owner_user_id=owner_user_id,
            event_type="mcp.grant_revoked",
            source_ref=f"mcp.grant_revoked:{owner_user_id}:{grant_id}",
        )
    except Exception:
        return


@router.get("", response_model=UserMCPToolGrantListResponse)
async def list_user_mcp_grants(request: Request) -> UserMCPToolGrantListResponse:
    runtime = request.app.state.runtime
    user = await require_authenticated_user(request)
    if runtime.user_mcp_config_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "mcp_feature_unavailable"},
        )
    grants = await runtime.storage.list_user_mcp_tool_grants(user.username, None)
    servers = {
        server.server_id: server
        for server in await runtime.user_mcp_config_service.list_servers(user.username)
    }
    return UserMCPToolGrantListResponse(
        grants=[
            UserMCPToolGrantResponse(
                grant_id=grant.grant_id,
                server_id=grant.server_id,
                server_display_name=(
                    servers[grant.server_id].display_name
                    if grant.server_id in servers
                    else "Unavailable MCP server"
                ),
                tool_name=grant.tool_name,
                granted_at=grant.granted_at,
                valid=grant.invalidated_at is None,
                invalid_reason=grant.invalid_reason,
            )
            for grant in grants
        ]
    )


@router.delete("/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_mcp_grant(grant_id: str, request: Request) -> Response:
    runtime = request.app.state.runtime
    user = await require_authenticated_user(request)
    deleted = await runtime.storage.delete_user_mcp_tool_grant_by_id(
        user.username,
        grant_id,
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "mcp_grant_not_found"},
        )
    await _audit_grant_revoked(request, user.username, grant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

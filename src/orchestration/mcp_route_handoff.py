from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MCP_SELECTED_ROUTE_NOT_AUTHORIZED = "mcp_selected_route_not_authorized"

_MCP_DISPATCH_CAPABILITY_ID = "mcp.dispatch"
_SELECTED_SERVER_BINDING_MODE = "explicit_command"
_ROUTE_ONLY_NODE_METADATA_KEYS = (
    "mcp_dispatch_server_id",
    "forced_by_mcp_command",
    "mcp_command",
)


@dataclass(frozen=True, slots=True)
class MCPRouteHandoffResult:
    normalized_node_metadata: Mapping[str, Any]
    rejection_code: str | None


def normalize_selected_mcp_route(
    *,
    capability_id: str,
    input_payload: Mapping[str, Any],
    node_metadata: Mapping[str, Any],
    pinned_server_id_present: bool,
    pinned_server_id: object,
    available_server_ids: frozenset[str],
) -> MCPRouteHandoffResult:
    original_metadata = dict(node_metadata)
    if capability_id != _MCP_DISPATCH_CAPABILITY_ID:
        return MCPRouteHandoffResult(original_metadata, None)

    if set(input_payload) != {"server_id"}:
        return MCPRouteHandoffResult(original_metadata, None)
    raw_server_id = input_payload.get("server_id")
    if not isinstance(raw_server_id, str) or not raw_server_id.strip():
        return MCPRouteHandoffResult(original_metadata, None)
    server_id = raw_server_id.strip()

    if pinned_server_id_present:
        if (
            not isinstance(pinned_server_id, str)
            or not pinned_server_id.strip()
            or pinned_server_id.strip() != server_id
        ):
            return MCPRouteHandoffResult(
                original_metadata,
                MCP_SELECTED_ROUTE_NOT_AUTHORIZED,
            )
    elif server_id not in available_server_ids:
        return MCPRouteHandoffResult(
            original_metadata,
            MCP_SELECTED_ROUTE_NOT_AUTHORIZED,
        )

    normalized_metadata = original_metadata
    for key in _ROUTE_ONLY_NODE_METADATA_KEYS:
        normalized_metadata.pop(key, None)
    normalized_metadata["mcp_binding_mode"] = _SELECTED_SERVER_BINDING_MODE
    return MCPRouteHandoffResult(normalized_metadata, None)

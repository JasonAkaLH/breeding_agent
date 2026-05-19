#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.integrations.mcp.client import MCPClient, MCPClientError
from src.integrations.mcp.config import load_mcp_server_config
from src.integrations.mcp.protocol import MCP_TRANSPORT_LEGACY_HTTP_SSE, MCP_TRANSPORT_STREAMABLE_HTTP
from src.integrations.mcp.transport_http import StreamableHTTPTransport
from src.integrations.mcp.transport_legacy_http_sse import LegacyHTTPSSETransport


def _base_server_report(server: Any, *, dry_run: bool) -> dict[str, Any]:
    tool_names = [tool.tool_name for tool in server.tools if tool.tool_name]
    return {
        "server_name": server.server_id,
        "requested_protocol_version": server.protocol_version,
        "negotiated_protocol_version": "not_executed_dry_run" if dry_run else "not_collected",
        "transport": server.transport,
        "adapter": "python_legacy",
        "serverInfo": "not_executed_dry_run" if dry_run else "not_collected",
        "capabilities": "not_executed_dry_run" if dry_run else "not_collected",
        "tools_list": tool_names,
        "safe_no_arg_tool_call_summary": {
            "status": "not_executed_dry_run" if dry_run else "not_executed_no_safe_tool_selected",
            "raw_payload": "omitted",
        },
        "diagnostics_redaction_evidence": {
            "request_header_names": list(server.request_header_names),
            "header_values": "redacted",
            "endpoint": "redacted",
            "raw_payload": "omitted",
        },
    }


def _transport_for(server: Any) -> Any:
    if server.transport == MCP_TRANSPORT_LEGACY_HTTP_SSE:
        return LegacyHTTPSSETransport(endpoint=server.endpoint, auth=server.auth, request_headers=server.request_headers)
    if server.transport == MCP_TRANSPORT_STREAMABLE_HTTP:
        return StreamableHTTPTransport(endpoint=server.endpoint, auth=server.auth, request_headers=server.request_headers)
    raise ValueError(f"Unsupported smoke transport: {server.transport}")


def _safe_shape(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"type": type(value).__name__}
    return {
        "keys": sorted(str(key) for key in value.keys()),
        "content_items": len(value.get("content") or ()) if isinstance(value.get("content"), list) else 0,
        "is_error": bool(value.get("isError", False)),
    }


async def _probe_server(server: Any, *, timeout_seconds: float) -> dict[str, Any]:
    report = _base_server_report(server, dry_run=False)
    transport = _transport_for(server)
    client = MCPClient(
        server_id=server.server_id,
        transport=transport,
        protocol_version=server.protocol_version,
        timeout_seconds=timeout_seconds,
        client_capabilities=server.client_capabilities,
        pinned_protocol_version=server.protocol_version_pinned,
        transport_family=server.transport,
    )
    try:
        init = await client.initialize()
        session = client.negotiated_session
        report["negotiated_protocol_version"] = session.negotiated_protocol_version if session else client.negotiated_protocol_version
        report["serverInfo"] = sorted(str(key) for key in dict(init.get("serverInfo") or {}).keys())
        report["capabilities"] = sorted(str(key) for key in dict(init.get("capabilities") or {}).keys())
        tools = await client.list_tools()
        report["tools_list"] = [str(tool.get("name")) for tool in tools if isinstance(tool, dict) and tool.get("name")]
        configured_no_arg = next((tool.tool_name for tool in server.tools if tool.tool_name and not tool.input_schema), "")
        if configured_no_arg:
            try:
                result = await client.call_tool(configured_no_arg, {})
                report["safe_no_arg_tool_call_summary"] = {
                    "status": "called_configured_no_arg_tool",
                    "tool_name": configured_no_arg,
                    "result_shape": _safe_shape(result),
                    "raw_payload": "omitted",
                }
            except MCPClientError as exc:
                report["safe_no_arg_tool_call_summary"] = {
                    "status": "call_failed_redacted",
                    "tool_name": configured_no_arg,
                    "error_code": exc.mcp_error_code,
                    "retriable": exc.retriable,
                    "raw_payload": "omitted",
                }
        return report
    except Exception as exc:  # noqa: BLE001 - smoke output must be redacted and non-normative.
        report["status"] = "probe_failed_redacted"
        report["error_type"] = type(exc).__name__
        report["diagnostics_redaction_evidence"]["exception_message"] = "omitted"
        return report
    finally:
        await client.close()


async def _build_report_async(*, config_path: Path, dry_run: bool, timeout_seconds: float) -> dict[str, Any]:
    config = load_mcp_server_config(path=config_path)
    if dry_run:
        servers = [_base_server_report(server, dry_run=True) for server in config.servers if server.enabled]
    else:
        servers = [await _probe_server(server, timeout_seconds=timeout_seconds) for server in config.servers if server.enabled]
    return {
        "external_smoke_sample": {
            "is_normative": False,
            "server_specific_logic_allowed": False,
            "dry_run": dry_run,
        },
        "servers": servers,
    }


def build_report(*, config_path: Path, dry_run: bool, timeout_seconds: float = 10) -> dict[str, Any]:
    return asyncio.run(_build_report_async(config_path=config_path, dry_run=dry_run, timeout_seconds=timeout_seconds))


def main() -> int:
    parser = argparse.ArgumentParser(description="Produce a redacted, non-normative MCP server config smoke report.")
    parser.add_argument("--config", required=True, help="Path to mcp_server_config.json")
    parser.add_argument("--dry-run", action="store_true", help="Do not contact external MCP servers; inspect config only.")
    parser.add_argument("--timeout-seconds", type=float, default=10.0, help="Per-request timeout for non-dry-run probes.")
    parser.add_argument("--json", action="store_true", help="Emit JSON report.")
    args = parser.parse_args()
    report = build_report(config_path=Path(args.config), dry_run=bool(args.dry_run), timeout_seconds=args.timeout_seconds)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.api.app import create_app  # noqa: E402
from src.core.enums import TaskStatus, UserMCPHealthStatus  # noqa: E402
from src.core.models import Conversation, Task  # noqa: E402


OWNER_ENV = "MAF_MCP_SMOKE_OWNER_USER_ID"
SERVER_ENV = "MAF_MCP_SMOKE_SERVER_ID"


async def build_report(runtime, *, owner_user_id: str, server_id: str) -> dict[str, object]:
    gateway = getattr(runtime, "user_mcp_gateway", None)
    signer = getattr(runtime, "_mcp_audit_reference_signer", None)
    if gateway is None or signer is None:
        raise RuntimeError("mcp_feature_unavailable")
    server = await runtime.storage.get_user_mcp_server(owner_user_id, server_id)
    if (
        server is None
        or not server.enabled
        or server.health_status != UserMCPHealthStatus.AVAILABLE
        or server.deletion_pending
        or server.deleted_at is not None
    ):
        raise RuntimeError("mcp_bound_server_unavailable")
    task_id = f"mcp-soft-binding-smoke-{uuid4().hex}"
    conversation_id = f"mcp-soft-binding-smoke-conversation-{uuid4().hex}"
    scope = None
    closed = False
    await runtime.storage.save_conversation(
        Conversation(
            conversation_id=conversation_id,
            username=owner_user_id,
            current_task_id=task_id,
        )
    )
    try:
        await runtime.storage.save_task(
            Task(
                task_id=task_id,
                conversation_id=conversation_id,
                root_message_id=f"mcp-soft-binding-smoke-message-{uuid4().hex}",
                status=TaskStatus.RUNNING,
            )
        )
        scope = await gateway.open_scope(
            SimpleNamespace(username=owner_user_id),
            task_id,
            server_id,
        )
        catalog = await gateway.list_tools(scope)
        protocol_version = str(catalog.effective_protocol_version)
        tool_count = len(catalog.tools)
    finally:
        try:
            if scope is not None:
                await gateway.close_scope(scope, "soft_binding_discover_only_smoke")
                closed = True
        finally:
            await runtime.storage.delete_conversation(conversation_id)
    return {
        "safe_server_ref": signer.safe_reference(
            server_id,
            context="mcp-server-binding-v1",
        ),
        "protocol_version": protocol_version,
        "discovery_succeeded": True,
        "tool_count": tool_count,
        "scope_closed": closed,
        "tool_call_executed": False,
        "attachment_transmitted": False,
    }


async def _run() -> tuple[int, dict[str, object]]:
    owner_user_id = str(os.environ.get(OWNER_ENV) or "").strip()
    server_id = str(os.environ.get(SERVER_ENV) or "").strip()
    if not owner_user_id or not server_id:
        return 2, {"status": "configuration_missing", "required_env": [OWNER_ENV, SERVER_ENV]}
    runtime = None
    try:
        runtime = create_app().state.runtime
        await runtime.start()
        report = await build_report(
            runtime,
            owner_user_id=owner_user_id,
            server_id=server_id,
        )
        return 0, {"status": "passed", **report}
    except Exception as exc:  # noqa: BLE001 - smoke output intentionally omits exception text.
        return 1, {
            "status": "failed_redacted",
            "error_type": type(exc).__name__,
            "error_code": str(getattr(exc, "code", "") or "mcp_smoke_failed"),
        }
    finally:
        if runtime is not None:
            try:
                await runtime.shutdown()
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a redacted user-scoped MCP fixed-server discover-only smoke."
    )
    parser.add_argument("--json", action="store_true", help="Emit a JSON report.")
    args = parser.parse_args()
    code, report = asyncio.run(_run())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(report["status"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())

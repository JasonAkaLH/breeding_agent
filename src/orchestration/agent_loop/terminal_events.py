from __future__ import annotations

import hashlib
import json

from src.core.enums import EventVisibility
from src.core.models import EventRecord

from .models import AgentItem, AgentRun


def build_agent_terminal_event(
    *,
    run: AgentRun,
    call_item: AgentItem,
    result_item: AgentItem,
) -> EventRecord:
    call_payload = json.loads(call_item.payload_json)
    result_payload = json.loads(result_item.payload_json)
    outcome = str(result_payload.get("outcome") or "")
    event_type = "node.completed" if outcome == "completed" else "node.failed"
    identity = hashlib.sha256(
        (
            f"maf.agent.terminal_event.v1\0{call_item.item_id}\0"
            f"{result_item.payload_sha256}\0{event_type}"
        ).encode("utf-8")
    ).hexdigest()
    safe_error_code = result_payload.get("safe_error_code")
    return EventRecord(
        event_id=f"agent-terminal-event:v1:{identity}",
        conversation_id=run.conversation_id,
        task_id=run.task_id,
        node_id=str(call_payload.get("node_id") or "") or None,
        event_type=event_type,
        payload={
            "call_item_id": call_item.item_id,
            "capability_id": str(call_payload.get("capability_id") or ""),
            "result_sha256": result_item.payload_sha256,
            **(
                {"code": safe_error_code}
                if event_type == "node.failed"
                and isinstance(safe_error_code, str)
                and safe_error_code
                else {}
            ),
        },
        visibility=EventVisibility.FRONTEND,
        created_at=result_item.committed_at or result_item.created_at,
    )

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any


_LOGGER = logging.getLogger(__name__)
_CONTROL_PATH = Path("runtime/agent-prompt-diagnostic.json")
_REQUEST_ID = re.compile(r"agent-sample:agent-run:(task-[a-z0-9-]+):r[0-9]+")


def capture_prompt_request(
    request_id: str, payload: dict[str, Any], *, attempt: int
) -> dict[str, Any] | None:
    """Observe one opt-in dev task without changing the provider request."""
    if os.environ.get("MAF_API_ENV") != "dev":
        return None
    match = _REQUEST_ID.fullmatch(request_id)
    if match is None:
        return None
    try:
        control = json.loads(_CONTROL_PATH.read_text(encoding="utf-8"))
        capture_id = control["capture_id"]
        if (
            not isinstance(capture_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", capture_id) is None
            or not 0 < float(control["expires_at"]) - time.time() <= 600
        ):
            return None
        task_id = match.group(1)
        claim = _CONTROL_PATH.with_name(f"agent-prompt-diagnostic-{capture_id}.task")
        try:
            descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if claim.read_text(encoding="utf-8") != task_id:
                return None
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(task_id)

        # Import only for an armed capture; the prompt constant remains the owner.
        from src.capabilities.main_agent.prompt_builder import MAIN_AGENT_FACTUAL_GROUNDING_LINES

        rules = MAIN_AGENT_FACTUAL_GROUNDING_LINES
        extra_body = payload.get("extra_body") or {}
        messages = extra_body.get("messages", payload["messages"])
        systems = [message for message in messages if message.get("role") == "system"]
        rule_indices = [
            [
                index
                for index, message in enumerate(messages)
                if message.get("role") == "system"
                and isinstance(message.get("content"), str)
                and rule in message["content"]
            ]
            for rule in rules
        ]
        context = {
            "capture_id": capture_id,
            "request_id": request_id,
            "task_id": task_id,
            "attempt": attempt,
        }
        _emit(
            {
                **context,
                "event": "agent_prompt_diagnostic.request",
                "boundary": "sdk_arguments",
                "model": extra_body.get("model", payload["model"]),
                "stream": extra_body.get("stream", payload["stream"]),
                "message_roles": [message.get("role") for message in messages],
                "grounding_rules_in_system": [bool(indices) for indices in rule_indices],
                "rule_system_message_indices": rule_indices,
                "expected_grounding_sha256": _digest(list(rules)),
                "system_prompt_sha256": _digest(systems),
                "extra_body_overrides_messages": "messages" in extra_body,
            }
        )
        return context
    except Exception:
        # Diagnostics must not prevent or retry an otherwise valid model call.
        return None


def capture_prompt_response(
    context: dict[str, Any] | None,
    *,
    response_id: object,
    finish_reason: object,
    tool_call_count: int,
) -> None:
    if context is None:
        return
    try:
        _emit(
            {
                **context,
                "event": "agent_prompt_diagnostic.response",
                "response_id": (
                    response_id
                    if isinstance(response_id, str)
                    and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", response_id)
                    else None
                ),
                "finish_reason": (
                    finish_reason
                    if finish_reason in {"stop", "tool_calls", "length", "content_filter", "function_call"}
                    else None
                ),
                "tool_call_count": tool_call_count,
            }
        )
    except Exception:
        return


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _emit(record: dict[str, Any]) -> None:
    _LOGGER.warning("agent_prompt_diagnostic %s", json.dumps(record, ensure_ascii=False, sort_keys=True))

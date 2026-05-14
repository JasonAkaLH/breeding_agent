from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

MCP_PROTOCOL_VERSION = "2025-11-25"
JSONRPC_VERSION = "2.0"

JSONRPC_REQUEST: dict[str, Any] = {
    "jsonrpc": JSONRPC_VERSION,
    "id": "req-1",
    "method": "tools/list",
    "params": {},
}
JSONRPC_NOTIFICATION: dict[str, Any] = {
    "jsonrpc": JSONRPC_VERSION,
    "method": "notifications/initialized",
    "params": {},
}
JSONRPC_RESPONSE: dict[str, Any] = {
    "jsonrpc": JSONRPC_VERSION,
    "id": "req-1",
    "result": {"ok": True},
}
JSONRPC_ERROR: dict[str, Any] = {
    "jsonrpc": JSONRPC_VERSION,
    "id": "req-1",
    "error": {"code": -32602, "message": "Invalid params"},
}
JSONRPC_BATCH = [copy.deepcopy(JSONRPC_REQUEST)]

SSE_MULTI_EVENT = (
    ": priming comment\n"
    "id: prime-1\n"
    "retry: 1500\n"
    "data:\n\n"
    ": heartbeat\n\n"
    "id: evt-1\n"
    "event: message\n"
    "data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\",\"params\":{\"progress\":1,\n"
    "data: \"progressToken\":\"tok-raw-1\"}}\n\n"
    "id: evt-2\n"
    "data: {\"jsonrpc\":\"2.0\",\"id\":\"call-1\",\"result\":{\"ok\":true}}\n\n"
)

CREATE_TASK_RESULT: dict[str, Any] = {
    "taskId": "mcp-task-raw-1",
    "status": {"state": "working", "message": "queued"},
    "_meta": {"io.modelcontextprotocol/related-task": {"taskId": "mcp-task-raw-1"}},
}

SENSITIVE_SAMPLE: dict[str, Any] = {
    "endpoint": "https://mcp.internal.example/rpc?token=secret",
    "Authorization": "Bearer secret-token",
    "MCP-Session-Id": "sess-raw-1",
    "Last-Event-ID": "evt-raw-1",
    "progressToken": "tok-raw-1",
    "taskId": "mcp-task-raw-1",
    "arguments": {"customer": "alice", "token": "nested-secret"},
}


class MCPFixtureError(AssertionError):
    """Raised when a fixture violates the locked MCP PRD contract."""


def clone_fixture(value: Any) -> Any:
    return copy.deepcopy(value)


def validate_jsonrpc_object_only(message: Any) -> Mapping[str, Any]:
    if isinstance(message, list):
        raise MCPFixtureError("JSON-RPC batch arrays are unsupported and must fail closed.")
    if not isinstance(message, Mapping):
        raise MCPFixtureError("JSON-RPC payload must be an object.")
    if message.get("jsonrpc") != JSONRPC_VERSION:
        raise MCPFixtureError("JSON-RPC payload must declare version 2.0.")
    has_id = "id" in message
    if has_id and "method" in message:
        return message
    if not has_id and "method" in message:
        return message
    if has_id and ("result" in message or "error" in message):
        return message
    raise MCPFixtureError("JSON-RPC object must be request, notification, response, or error.")


@dataclass(slots=True, frozen=True)
class ParsedSSEEvent:
    event: str | None
    event_id: str | None
    retry_ms: int | None
    data: str
    json_payload: Mapping[str, Any] | None
    is_priming: bool = False


def parse_sse_events(text: str, *, max_event_bytes: int = 256 * 1024) -> list[ParsedSSEEvent]:
    events: list[ParsedSSEEvent] = []
    event_name: str | None = None
    event_id: str | None = None
    retry_ms: int | None = None
    data_lines: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal event_name, event_id, retry_ms, data_lines, size
        if event_name is None and event_id is None and retry_ms is None and not data_lines:
            return
        data = "\n".join(data_lines)
        if data == "":
            events.append(ParsedSSEEvent(event_name, event_id, retry_ms, data, None, is_priming=True))
        else:
            payload = json.loads(data)
            if not isinstance(payload, Mapping):
                raise MCPFixtureError("Non-empty SSE data must decode to a JSON-RPC object.")
            validate_jsonrpc_object_only(payload)
            events.append(ParsedSSEEvent(event_name, event_id, retry_ms, data, payload, is_priming=False))
        event_name = None
        event_id = None
        retry_ms = None
        data_lines = []
        size = 0

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        size += len(line.encode("utf-8")) + 1
        if size > max_event_bytes:
            raise MCPFixtureError("SSE event exceeded the maximum allowed size.")
        if line == "":
            flush()
            continue
        if line.startswith(":"):
            continue
        field_name, sep, value = line.partition(":")
        if not sep:
            continue
        value = value[1:] if value.startswith(" ") else value
        if field_name == "data":
            data_lines.append(value)
        elif field_name == "id":
            event_id = value
        elif field_name == "event":
            event_name = value
        elif field_name == "retry":
            retry_ms = int(value)
    flush()
    return events


@dataclass(slots=True, frozen=True)
class FakeHTTPResponse:
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    sse_events: tuple[ParsedSSEEvent, ...] = ()


class FakeStreamableHTTPTransport:
    """Small fake for PRD conformance tests; it does not touch production runtime code."""

    def __init__(self) -> None:
        self.session_id = "sess-raw-1"
        self.requests: list[dict[str, Any]] = []
        self.reinitialize_count = 0

    def post(self, message: Any, *, headers: Mapping[str, str] | None = None, sse_text: str | None = None, status_code: int = 200) -> FakeHTTPResponse:
        headers = {str(k): str(v) for k, v in dict(headers or {}).items()}
        self._assert_common_post_headers(headers)
        payload = validate_jsonrpc_object_only(message)
        self.requests.append({"method": "POST", "message": copy.deepcopy(payload), "headers": headers})
        if status_code == 404:
            self.reinitialize_count += 1
            self.session_id = "sess-reinitialized-1"
            return FakeHTTPResponse(404, headers={})
        if "id" not in payload or ("id" in payload and ("result" in payload or "error" in payload)):
            return FakeHTTPResponse(202, headers={}, body=b"")
        if sse_text is not None:
            events = tuple(parse_sse_events(sse_text))
            return FakeHTTPResponse(200, headers={"content-type": "text/event-stream"}, sse_events=events)
        return FakeHTTPResponse(200, headers={"content-type": "application/json"}, body=json.dumps(JSONRPC_RESPONSE).encode("utf-8"))

    def get_stream(self, *, headers: Mapping[str, str] | None = None, status_code: int = 200) -> FakeHTTPResponse:
        headers = {str(k): str(v) for k, v in dict(headers or {}).items()}
        if headers.get("Accept") != "text/event-stream":
            raise MCPFixtureError("GET stream must request text/event-stream.")
        self.requests.append({"method": "GET", "headers": headers})
        if status_code == 405:
            return FakeHTTPResponse(405, body=b"")
        return FakeHTTPResponse(200, headers={"content-type": "text/event-stream"})

    def delete_session(self, *, headers: Mapping[str, str] | None = None, status_code: int = 405) -> FakeHTTPResponse:
        headers = {str(k): str(v) for k, v in dict(headers or {}).items()}
        self.requests.append({"method": "DELETE", "headers": headers})
        if status_code == 405:
            return FakeHTTPResponse(405, body=b"")
        return FakeHTTPResponse(204, body=b"")

    @staticmethod
    def _assert_common_post_headers(headers: Mapping[str, str]) -> None:
        accept = headers.get("Accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            raise MCPFixtureError("POST Accept must allow JSON and SSE responses.")
        if headers.get("Content-Type") != "application/json":
            raise MCPFixtureError("POST Content-Type must be application/json.")
        if headers.get("MCP-Protocol-Version") != MCP_PROTOCOL_VERSION:
            raise MCPFixtureError("POST must carry the MCP protocol version header.")


def decide_task_augmented_call(*, server_tools_call_tasks: bool, tool_task_support: str | None, mode: str) -> str:
    support = tool_task_support or "forbidden"
    if support not in {"required", "optional", "forbidden"}:
        raise MCPFixtureError(f"Unknown taskSupport value: {support}")
    if mode not in {"required", "preferred", "disabled"}:
        raise MCPFixtureError(f"Unknown task augmented mode: {mode}")
    if not server_tools_call_tasks:
        return "fail_closed" if mode == "required" else "plain_call"
    if support == "required":
        return "fail_closed" if mode == "disabled" else "task_augmented"
    if support == "optional":
        if mode == "required":
            return "task_augmented"
        if mode == "preferred":
            return "task_augmented_preferred"
        return "plain_call"
    if mode == "required":
        return "fail_closed"
    return "plain_call"


class ProgressTracker:
    def __init__(self, token: str | int) -> None:
        if not isinstance(token, (str, int)):
            raise MCPFixtureError("progress token must be a string or integer.")
        self.token = token
        self.last_progress: float | int | None = None

    def accept(self, progress: float | int) -> None:
        if self.last_progress is not None and progress < self.last_progress:
            raise MCPFixtureError("progress must be monotonic for the token lifecycle.")
        self.last_progress = progress


def make_safe_ref(*, server_id: str, tool_name: str, task_index: int) -> str:
    return f"mcp-task:{server_id}:{tool_name}:{task_index:032x}"


_SENSITIVE_KEY_RE = re.compile(r"authorization|token|session|event|endpoint|taskid|arguments", re.IGNORECASE)


def redact_for_frontend(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if _SENSITIVE_KEY_RE.search(str(key)) else redact_for_frontend(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_for_frontend(item) for item in value]
    return value


def assert_no_raw_sensitive_values(value: Any, raw_values: Mapping[str, str]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for name, raw_value in raw_values.items():
        if raw_value and raw_value in encoded:
            raise MCPFixtureError(f"raw sensitive value leaked: {name}")

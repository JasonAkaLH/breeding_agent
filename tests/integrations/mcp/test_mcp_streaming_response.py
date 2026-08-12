from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src.integrations.mcp.client import MCPProtocolError
from src.integrations.mcp.streaming_response import parse_json_rpc_byte_stream, parse_sse_json_rpc_byte_stream
from src.integrations.mcp.temporary_results import MCPTemporaryResultStore
from src.integrations.mcp.transport_http import StreamableHTTPTransport


async def _chunks(payload: bytes, sizes: tuple[int, ...] = (1, 2, 5, 3)):
    offset = 0
    index = 0
    while offset < len(payload):
        size = sizes[index % len(sizes)]
        yield payload[offset : offset + size]
        offset += size
        index += 1


class _StreamingOnlyResponse:
    status_code = 200

    def __init__(self, payload: bytes, *, content_type: str) -> None:
        self.headers = {"content-type": content_type, "MCP-Session-Id": "session-1"}
        self._payload = payload

    @property
    def content(self):
        raise AssertionError("streaming path accessed response.content")

    @property
    def text(self):
        raise AssertionError("streaming path accessed response.text")

    def json(self):
        raise AssertionError("streaming path accessed response.json()")

    async def aiter_bytes(self):
        async for chunk in _chunks(self._payload):
            yield chunk


class _StreamContext:
    def __init__(self, response: _StreamingOnlyResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _StreamingOnlyClient:
    def __init__(self, response: _StreamingOnlyResponse) -> None:
        self._response = response
        self.calls = []

    def stream(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))
        return _StreamContext(self._response)


class _PolicyBoundConnection:
    def __init__(self, endpoint_url: str, client: _StreamingOnlyClient) -> None:
        self.endpoint_url = endpoint_url
        self.client = client


class MCPStreamingResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_incremental_json_extracts_result_and_preserves_sha256(self) -> None:
        result_payload = {"content": [{"type": "text", "text": "x" * 5000}], "isError": False}
        result_bytes = json.dumps(result_payload, separators=(",", ":")).encode()
        envelope = b'{"jsonrpc":"2.0","id":"call-1","result":' + result_bytes + b'}'
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=32)
            parsed = await parse_json_rpc_byte_stream(_chunks(envelope), store.create_sink("task-1"))
            rebuilt = b"".join([chunk async for chunk in store.iter_bytes(parsed.result_ref)])

        self.assertEqual(rebuilt, result_bytes)
        self.assertEqual(parsed.result_ref.sha256, hashlib.sha256(result_bytes).hexdigest())
        self.assertEqual(parsed.result_ref.storage, "file")
        self.assertEqual(parsed.message["id"], "call-1")
        self.assertEqual(parsed.message["result"]["_mcpResultRef"]["ref"], parsed.result_ref.ref)

    async def test_control_outcome_remains_materialized_for_2026_adapter(self) -> None:
        result = {
            "resultType": "input_required",
            "inputRequests": [{"id": "confirm", "kind": "boolean"}],
            "requestState": "opaque-state",
        }
        envelope = json.dumps(
            {"jsonrpc": "2.0", "id": 3, "result": result},
            separators=(",", ":"),
        ).encode()
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=4)
            parsed = await parse_json_rpc_byte_stream(
                _chunks(envelope),
                store.create_sink("task-control"),
                control_result_types=frozenset({"input_required", "task"}),
            )

        self.assertIsNone(parsed.result_ref)
        self.assertEqual(parsed.message["result"], result)

    async def test_large_control_outcome_is_not_misclassified_as_completed(self) -> None:
        result = {
            "resultType": "input_required",
            "inputRequests": [
                {"id": "details", "kind": "text", "description": "x" * 70_000}
            ],
            "requestState": "opaque-state",
        }
        envelope = json.dumps(
            {"jsonrpc": "2.0", "id": 3, "result": result},
            separators=(",", ":"),
        ).encode()
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=4)
            parsed = await parse_json_rpc_byte_stream(
                _chunks(envelope),
                store.create_sink("task-large-control"),
                control_result_types=frozenset({"input_required", "task"}),
            )

        self.assertIsNone(parsed.result_ref)
        self.assertEqual(parsed.message["result"]["resultType"], "input_required")
        self.assertEqual(
            len(parsed.message["result"]["inputRequests"][0]["description"]),
            70_000,
        )

    async def test_legacy_business_result_type_is_always_streamed(self) -> None:
        result = {"resultType": "task", "payload": "x" * 1_100_000}
        result_bytes = json.dumps(result, separators=(",", ":")).encode()
        envelope = b'{"jsonrpc":"2.0","id":4,"result":' + result_bytes + b"}"
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=16)
            parsed = await parse_json_rpc_byte_stream(
                _chunks(envelope), store.create_sink("task-legacy-result-type")
            )
            rebuilt = b"".join(
                [chunk async for chunk in store.iter_bytes(parsed.result_ref)]
            )

        self.assertEqual(rebuilt, result_bytes)
        self.assertEqual(parsed.result_ref.storage, "file")

    async def test_incremental_sse_handles_socket_boundaries_without_buffering_result_line(self) -> None:
        result_bytes = b'{"content":[{"type":"text","text":"streamed"}]}'
        envelope = b'{"jsonrpc":"2.0","id":7,"result":' + result_bytes + b'}'
        sse = b"event: message\r\nid: event-7\r\ndata: " + envelope + b"\r\n\r\n"
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=4)
            parsed = await parse_sse_json_rpc_byte_stream(_chunks(sse), store.create_sink("task-7"))
            rebuilt = b"".join([chunk async for chunk in store.iter_bytes(parsed.result_ref)])

        self.assertEqual(rebuilt, result_bytes)
        self.assertEqual(parsed.event.event_id, "event-7")
        self.assertEqual(parsed.event.event, "message")

    async def test_large_sse_business_result_spills_to_file(self) -> None:
        result_bytes = json.dumps(
            {"content": [{"type": "text", "text": "x" * 200_000}]},
            separators=(",", ":"),
        ).encode()
        sse = (
            b"data: {\"jsonrpc\":\"2.0\",\"id\":8,\"result\":"
            + result_bytes
            + b"}\n\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=16)
            parsed = await parse_sse_json_rpc_byte_stream(
                _chunks(sse), store.create_sink("task-large-sse")
            )
            rebuilt = b"".join(
                [chunk async for chunk in store.iter_bytes(parsed.result_ref)]
            )

        self.assertEqual(rebuilt, result_bytes)
        self.assertEqual(parsed.result_ref.storage, "file")

    async def test_malformed_or_cancelled_stream_aborts_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = MCPTemporaryResultStore(root, memory_threshold_bytes=1)
            with self.assertRaises(MCPProtocolError):
                await parse_json_rpc_byte_stream(
                    _chunks(b'{"jsonrpc":"2.0","id":1,"result":{"partial":true'),
                    store.create_sink("malformed"),
                )
            self.assertEqual([path for path in root.rglob("*") if path.is_file()], [])

            async def cancelled():
                yield b'{"jsonrpc":"2.0","id":1,"result":{"partial":"'
                raise asyncio.CancelledError

            with self.assertRaises(asyncio.CancelledError):
                await parse_json_rpc_byte_stream(cancelled(), store.create_sink("cancelled"))
            self.assertEqual([path for path in root.rglob("*") if path.is_file()], [])

    async def test_transport_streaming_path_never_accesses_buffered_response_properties(self) -> None:
        result_bytes = b'{"content":[{"type":"text","text":"ok"}]}'
        envelope = b'{"jsonrpc":"2.0","id":1,"result":' + result_bytes + b'}'
        response = _StreamingOnlyResponse(envelope, content_type="application/json")
        client = _StreamingOnlyClient(response)
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=4)
            transport = StreamableHTTPTransport(endpoint="https://mcp.example.test", client=client)  # type: ignore[arg-type]
            parsed = await transport.send_streaming(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}},
                protocol_version="2025-11-25",
                result_sink=store.create_sink("task-1"),
            )
            ref_payload = parsed.message["result"]["_mcpResultRef"]

        self.assertEqual(ref_payload["sha256"], hashlib.sha256(result_bytes).hexdigest())
        self.assertEqual(client.calls[0][0], "POST")

    async def test_transport_accepts_policy_bound_connection_without_owning_policy(self) -> None:
        result_bytes = b'{"ok":true}'
        response = _StreamingOnlyResponse(
            b'{"jsonrpc":"2.0","id":1,"result":' + result_bytes + b'}',
            content_type="application/json",
        )
        client = _StreamingOnlyClient(response)
        connection = _PolicyBoundConnection("https://bound.example.test/mcp", client)
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=4)
            transport = StreamableHTTPTransport(policy_bound_connection=connection)  # type: ignore[arg-type]
            await transport.send_streaming(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}},
                protocol_version="2025-11-25",
                result_sink=store.create_sink("task-1"),
            )

        self.assertEqual(client.calls[0][1], "https://bound.example.test/mcp")

    async def test_transport_streams_request_scoped_sse_without_buffered_properties(self) -> None:
        result_bytes = b'{"content":[{"type":"text","text":"sse ok"}]}'
        envelope = b'{"jsonrpc":"2.0","id":9,"result":' + result_bytes + b'}'
        response = _StreamingOnlyResponse(
            b"event: message\nid: event-9\ndata: " + envelope + b"\n\n",
            content_type="text/event-stream",
        )
        client = _StreamingOnlyClient(response)
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=4)
            transport = StreamableHTTPTransport(endpoint="https://mcp.example.test", client=client)  # type: ignore[arg-type]
            parsed = await transport.send_streaming(
                {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {}},
                protocol_version="2025-11-25",
                result_sink=store.create_sink("task-9", scope_id="scope-9"),
            )

        self.assertEqual(parsed.last_event_id, "event-9")
        self.assertEqual(parsed.message["result"]["_mcpResultRef"]["sha256"], hashlib.sha256(result_bytes).hexdigest())

    async def test_transport_streams_notification_before_final_sse_response(self) -> None:
        notification = b'{"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}'
        result_bytes = b'{"content":[{"type":"text","text":"done"}]}'
        response_message = b'{"jsonrpc":"2.0","id":11,"result":' + result_bytes + b'}'
        response = _StreamingOnlyResponse(
            b"data: " + notification + b"\n\ndata: " + response_message + b"\n\n",
            content_type="text/event-stream",
        )
        client = _StreamingOnlyClient(response)
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=4)
            transport = StreamableHTTPTransport(
                endpoint="https://mcp.example.test", client=client  # type: ignore[arg-type]
            )
            parsed = await transport.send_streaming(
                {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {}},
                protocol_version="2026-07-28",
                result_sink=store.create_sink("task-11", scope_id="scope-11"),
            )

        self.assertEqual(len(parsed.sse_events), 2)
        self.assertEqual(parsed.sse_events[0].message["method"], "notifications/progress")
        self.assertEqual(
            parsed.message["result"]["_mcpResultRef"]["sha256"],
            hashlib.sha256(result_bytes).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()

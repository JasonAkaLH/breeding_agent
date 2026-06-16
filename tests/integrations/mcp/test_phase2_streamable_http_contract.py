from __future__ import annotations

import unittest

from tests.fixtures.mcp.protocol_fixtures import (
    JSONRPC_NOTIFICATION,
    JSONRPC_RESPONSE,
    MCP_PROTOCOL_VERSION,
    MCPFixtureError,
    FakeStreamableHTTPTransport,
    SSE_MULTI_EVENT,
    clone_fixture,
    parse_sse_events,
)


POST_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
    "MCP-Session-Id": "sess-raw-1",
}


class MCPPhase2StreamableHTTPContractTests(unittest.TestCase):
    def test_sse_parser_handles_priming_comments_id_retry_and_multiple_jsonrpc_events(self) -> None:
        events = parse_sse_events(SSE_MULTI_EVENT)

        self.assertEqual(len(events), 3)
        self.assertTrue(events[0].is_priming)
        self.assertEqual(events[0].event_id, "prime-1")
        self.assertEqual(events[0].retry_ms, 1500)
        self.assertEqual(events[1].event_id, "evt-1")
        self.assertEqual(events[1].event, "message")
        self.assertEqual(events[1].json_payload["method"], "notifications/progress")
        self.assertEqual(events[2].json_payload["result"], {"ok": True})

    def test_sse_parser_rejects_non_object_jsonrpc_data_and_oversized_events(self) -> None:
        with self.assertRaisesRegex(MCPFixtureError, "object"):
            parse_sse_events("data: [1, 2]\n\n")

        with self.assertRaisesRegex(MCPFixtureError, "maximum"):
            parse_sse_events("data: " + ("x" * 32) + "\n\n", max_event_bytes=8)

    def test_post_notification_and_response_receive_202_no_body(self) -> None:
        transport = FakeStreamableHTTPTransport()

        notification_response = transport.post(clone_fixture(JSONRPC_NOTIFICATION), headers=POST_HEADERS)
        response_response = transport.post(clone_fixture(JSONRPC_RESPONSE), headers=POST_HEADERS)

        self.assertEqual(notification_response.status_code, 202)
        self.assertEqual(notification_response.body, b"")
        self.assertEqual(response_response.status_code, 202)
        self.assertEqual(response_response.body, b"")

    def test_session_404_get_405_and_delete_405_are_compatibility_paths(self) -> None:
        transport = FakeStreamableHTTPTransport()

        expired = transport.post(
            {"jsonrpc": "2.0", "id": "req-404", "method": "tools/list", "params": {}},
            headers=POST_HEADERS,
            status_code=404,
        )
        get_unsupported = transport.get_stream(headers={"Accept": "text/event-stream", "Last-Event-ID": "evt-1"}, status_code=405)
        delete_unsupported = transport.delete_session(headers={"MCP-Session-Id": "sess-reinitialized-1"}, status_code=405)

        self.assertEqual(expired.status_code, 404)
        self.assertEqual(transport.reinitialize_count, 1)
        self.assertEqual(transport.session_id, "sess-reinitialized-1")
        self.assertEqual(get_unsupported.status_code, 405)
        self.assertEqual(get_unsupported.body, b"")
        self.assertEqual(delete_unsupported.status_code, 405)
        self.assertEqual(delete_unsupported.body, b"")


if __name__ == "__main__":
    unittest.main()

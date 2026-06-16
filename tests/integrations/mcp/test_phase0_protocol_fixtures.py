from __future__ import annotations

import unittest

from tests.fixtures.mcp.protocol_fixtures import (
    CREATE_TASK_RESULT,
    JSONRPC_BATCH,
    JSONRPC_ERROR,
    JSONRPC_NOTIFICATION,
    JSONRPC_REQUEST,
    JSONRPC_RESPONSE,
    MCPFixtureError,
    SENSITIVE_SAMPLE,
    assert_no_raw_sensitive_values,
    clone_fixture,
    redact_for_frontend,
    validate_jsonrpc_object_only,
)


class MCPPhase0ProtocolFixtureTests(unittest.TestCase):
    def test_jsonrpc_fixtures_cover_object_message_shapes(self) -> None:
        cases = [JSONRPC_REQUEST, JSONRPC_NOTIFICATION, JSONRPC_RESPONSE, JSONRPC_ERROR]

        for case in cases:
            with self.subTest(case=case):
                validated = validate_jsonrpc_object_only(clone_fixture(case))
                self.assertIs(validated["jsonrpc"], case["jsonrpc"])

        self.assertIn("method", JSONRPC_REQUEST)
        self.assertNotIn("id", JSONRPC_NOTIFICATION)
        self.assertIn("result", JSONRPC_RESPONSE)
        self.assertIn("error", JSONRPC_ERROR)

    def test_jsonrpc_batch_arrays_are_rejected_fail_closed(self) -> None:
        with self.assertRaisesRegex(MCPFixtureError, "batch arrays"):
            validate_jsonrpc_object_only(clone_fixture(JSONRPC_BATCH))

    def test_phase0_task_and_redaction_fixtures_do_not_leak_raw_values(self) -> None:
        frontend_payload = {
            "type": "mcp.long_task_started",
            "safe_ref": "mcp-task:crm:search_customer:00000000000000000000000000000001",
            "task": clone_fixture(CREATE_TASK_RESULT),
            "transport": clone_fixture(SENSITIVE_SAMPLE),
        }

        redacted = redact_for_frontend(frontend_payload)

        self.assertEqual(redacted["safe_ref"], "mcp-task:crm:search_customer:00000000000000000000000000000001")
        self.assertEqual(redacted["transport"]["Authorization"], "<redacted>")
        self.assertEqual(redacted["transport"]["progressToken"], "<redacted>")
        assert_no_raw_sensitive_values(
            redacted,
            {
                "endpoint": "https://mcp.internal.example/rpc?token=secret",
                "auth": "secret-token",
                "session": "sess-raw-1",
                "event": "evt-raw-1",
                "progress": "tok-raw-1",
                "task": "mcp-task-raw-1",
                "nested": "nested-secret",
            },
        )


if __name__ == "__main__":
    unittest.main()

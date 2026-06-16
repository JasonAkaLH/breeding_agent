from __future__ import annotations

import unittest

from tests.fixtures.mcp.protocol_fixtures import (
    MCPFixtureError,
    ProgressTracker,
    assert_no_raw_sensitive_values,
    decide_task_augmented_call,
    make_safe_ref,
    redact_for_frontend,
)


class MCPPhase3TasksContractTests(unittest.TestCase):
    def test_task_support_negotiation_treats_missing_as_forbidden(self) -> None:
        matrix = [
            (False, None, "required", "fail_closed"),
            (False, "optional", "preferred", "plain_call"),
            (True, "required", "disabled", "fail_closed"),
            (True, "required", "preferred", "task_augmented"),
            (True, "optional", "required", "task_augmented"),
            (True, "optional", "preferred", "task_augmented_preferred"),
            (True, "optional", "disabled", "plain_call"),
            (True, "forbidden", "required", "fail_closed"),
            (True, "forbidden", "preferred", "plain_call"),
            (True, None, "preferred", "plain_call"),
        ]

        for server_support, tool_support, mode, expected in matrix:
            with self.subTest(server_support=server_support, tool_support=tool_support, mode=mode):
                self.assertEqual(
                    decide_task_augmented_call(
                        server_tools_call_tasks=server_support,
                        tool_task_support=tool_support,
                        mode=mode,
                    ),
                    expected,
                )

    def test_progress_token_accepts_string_or_integer_and_requires_monotonic_progress(self) -> None:
        string_tracker = ProgressTracker("progress-token-1")
        integer_tracker = ProgressTracker(42)

        string_tracker.accept(0)
        string_tracker.accept(10)
        string_tracker.accept(10)
        integer_tracker.accept(1)

        with self.assertRaisesRegex(MCPFixtureError, "monotonic"):
            string_tracker.accept(9)
        with self.assertRaisesRegex(MCPFixtureError, "string or integer"):
            ProgressTracker({"bad": "token"})

    def test_frontend_progress_event_uses_safe_ref_and_redacts_raw_ids(self) -> None:
        raw_values = {
            "task_id": "raw-task-123",
            "session_id": "raw-session-456",
            "event_id": "raw-event-789",
            "progress_token": "raw-progress-token",
        }
        event = {
            "type": "mcp.long_task_progress",
            "safe_ref": make_safe_ref(server_id="crm", tool_name="search_customer", task_index=7),
            "taskId": raw_values["task_id"],
            "MCP-Session-Id": raw_values["session_id"],
            "Last-Event-ID": raw_values["event_id"],
            "progressToken": raw_values["progress_token"],
            "progress": 20,
        }

        redacted = redact_for_frontend(event)

        self.assertEqual(redacted["safe_ref"], "mcp-task:crm:search_customer:00000000000000000000000000000007")
        self.assertEqual(redacted["progress"], 20)
        self.assertEqual(redacted["taskId"], "<redacted>")
        self.assertEqual(redacted["progressToken"], "<redacted>")
        assert_no_raw_sensitive_values(redacted, raw_values)


if __name__ == "__main__":
    unittest.main()

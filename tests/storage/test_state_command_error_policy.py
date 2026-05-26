from __future__ import annotations

import unittest

from src.state.errors import StatePlatformError, classify_state_error
from src.state.service import should_retry_command_error


class StateCommandErrorPolicyTest(unittest.TestCase):
    def test_handler_retry_policy_only_retries_allowlisted_transient_errors(self) -> None:
        retryable = classify_state_error(type("E", (Exception,), {"sqlstate": "40P01"})("deadlock"), operation="handler")
        self.assertTrue(should_retry_command_error(retryable))
        business = StatePlatformError("handler_contract_error", "bad payload", retryable=False)
        self.assertFalse(should_retry_command_error(business))
        unknown = classify_state_error(RuntimeError("unknown"), operation="handler")
        self.assertFalse(should_retry_command_error(unknown))

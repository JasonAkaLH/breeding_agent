from __future__ import annotations

import inspect
import unittest

from src.integrations.mcp import runtime_state, tasks
from src.integrations.mcp.rust_contract import contract_value, load_mcp_runtime_contract, status_list


class MCPRustRuntimeContractTest(unittest.TestCase):
    def test_mcp_task_status_policy_comes_from_rust_contract_artifact(self) -> None:
        contract = load_mcp_runtime_contract()
        self.assertEqual(contract["component"], "maf_mcp_runtime_sidecar")
        self.assertEqual(status_list("task_terminal_states"), frozenset({"completed", "failed", "cancelled"}))
        self.assertEqual(contract_value("task_cancelled_state"), "cancelled")
        self.assertEqual(contract_value("task_completed_state"), "completed")
        self.assertEqual(contract_value("task_failed_state"), "failed")
        self.assertEqual(contract_value("task_input_required_state"), "input_required")
        self.assertEqual(contract_value("task_default_state"), "working")
        self.assertEqual(contract_value("related_task_meta_key"), "io.modelcontextprotocol/related-task")

    def test_python_mcp_runtime_has_no_inline_task_terminal_state_set(self) -> None:
        tasks_source = inspect.getsource(tasks)
        runtime_source = inspect.getsource(runtime_state.MCPRuntimeState.cancel_platform_task)
        self.assertNotIn('frozenset({"completed", "failed", "cancelled"})', tasks_source)
        self.assertNotIn('{"completed", "failed", "cancelled"}', runtime_source)
        self.assertNotIn('{"failed", "cancelled"}', inspect.getsource(runtime_state.MCPRuntimeState._resolve_task_result))
        self.assertIn('task_terminal_states', tasks_source)
        self.assertIn('task_terminal_states', runtime_source)


if __name__ == "__main__":
    unittest.main()

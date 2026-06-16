from __future__ import annotations

import json
import sys
import types
import unittest
from typing import Any
from unittest.mock import patch

from src.core.enums import TaskStatus
from src.core.models import Task
from src.lifecycle.errors import LifecycleRustContractError
from src.lifecycle.rust_contract import load_lifecycle_contract, transition_allowed
from src.lifecycle.task_state_machine import can_accept_late_result


class LifecyclePyo3FacadeTest(unittest.TestCase):
    def tearDown(self) -> None:
        load_lifecycle_contract.cache_clear()
        sys.modules.pop("fake_maf_core_lifecycle_pyo3", None)
        sys.modules.pop("bad_maf_core_lifecycle_pyo3", None)

    def test_enforce_requires_prebuilt_pyo3_module(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_LIFECYCLE_MODE": "enforce",
                "MAF_CORE_LIFECYCLE_PYO3_MODULE": "missing_maf_core_lifecycle_pyo3",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(LifecycleRustContractError, "prebuilt PyO3"):
                load_lifecycle_contract()

    def test_enforce_fails_closed_on_lifecycle_contract_mismatch(self) -> None:
        module = types.ModuleType("bad_maf_core_lifecycle_pyo3")
        contract = _lifecycle_contract()
        contract["transition_table_hash"] = "wrong"
        module.lifecycle_contract_json = lambda: json.dumps(contract)
        module.lifecycle_can_transition_json = lambda payload: json.dumps({"allowed": True, "error": None})
        sys.modules[module.__name__] = module

        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_LIFECYCLE_MODE": "enforce",
                "MAF_CORE_LIFECYCLE_PYO3_MODULE": module.__name__,
            },
            clear=False,
        ):
            with self.assertRaisesRegex(LifecycleRustContractError, "contract mismatch"):
                load_lifecycle_contract()

    def test_shadow_keeps_checked_in_artifact_on_lifecycle_contract_mismatch(self) -> None:
        module = types.ModuleType("bad_maf_core_lifecycle_pyo3")
        contract = _lifecycle_contract()
        contract["transition_table_hash"] = "wrong"
        module.lifecycle_contract_json = lambda: json.dumps(contract)
        sys.modules[module.__name__] = module

        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_LIFECYCLE_MODE": "shadow",
                "MAF_CORE_LIFECYCLE_PYO3_MODULE": module.__name__,
            },
            clear=False,
        ):
            loaded = load_lifecycle_contract()

        self.assertEqual(loaded["transition_table_hash"], _lifecycle_contract()["transition_table_hash"])

    def test_enforce_transition_allowed_calls_rust_pyo3_kernel(self) -> None:
        calls: list[dict[str, str]] = []
        module = _fake_module()

        def can_transition(payload: str) -> str:
            calls.append(json.loads(payload))
            return json.dumps({"allowed": True, "error": None})

        module.lifecycle_can_transition_json = can_transition
        sys.modules[module.__name__] = module

        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_LIFECYCLE_MODE": "enforce",
                "MAF_CORE_LIFECYCLE_PYO3_MODULE": module.__name__,
            },
            clear=False,
        ):
            self.assertTrue(transition_allowed("node.begin_resume", "ready_to_resume"))

        self.assertEqual(calls, [{"current": "ready_to_resume", "operation": "node.begin_resume"}])

    def test_enforce_rejects_malformed_rust_transition_response(self) -> None:
        module = _fake_module()
        module.lifecycle_can_transition_json = lambda payload: "{}"
        sys.modules[module.__name__] = module

        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_LIFECYCLE_MODE": "enforce",
                "MAF_CORE_LIFECYCLE_PYO3_MODULE": module.__name__,
            },
            clear=False,
        ):
            with self.assertRaisesRegex(LifecycleRustContractError, "invalid transition response"):
                transition_allowed("node.begin_resume", "ready_to_resume")

    def test_task_late_result_policy_uses_rust_pyo3_kernel_in_enforce(self) -> None:
        calls: list[dict[str, str | None]] = []
        module = _fake_module()

        def can_accept(payload: str) -> str:
            calls.append(json.loads(payload))
            return json.dumps({"allowed": False, "error": None})

        module.lifecycle_can_accept_late_result_json = can_accept
        sys.modules[module.__name__] = module
        task = Task(task_id="task-1", conversation_id="conv", root_message_id="msg", status=TaskStatus.RUNNING)

        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_LIFECYCLE_MODE": "enforce",
                "MAF_CORE_LIFECYCLE_PYO3_MODULE": module.__name__,
            },
            clear=False,
        ):
            self.assertFalse(can_accept_late_result(task))

        self.assertEqual(calls, [{"task_status": "running"}])


def _fake_module() -> types.ModuleType:
    module = types.ModuleType("fake_maf_core_lifecycle_pyo3")
    module.lifecycle_contract_json = lambda: json.dumps(_lifecycle_contract())
    module.lifecycle_can_transition_json = lambda payload: json.dumps({"allowed": True, "error": None})
    module.lifecycle_transition_target_json = lambda payload: json.dumps({"target": "resuming", "error": None})
    module.lifecycle_cancel_node_target_json = lambda payload: json.dumps({"target": None, "error": None})
    module.lifecycle_can_accept_late_result_json = lambda payload: json.dumps({"allowed": True, "error": None})
    return module


def _lifecycle_contract() -> dict[str, Any]:
    load_lifecycle_contract.cache_clear()
    with patch.dict("os.environ", {"MAF_RUST_LIFECYCLE_MODE": "off"}, clear=False):
        return dict(load_lifecycle_contract())


if __name__ == "__main__":
    unittest.main()

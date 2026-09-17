from __future__ import annotations

import dataclasses
import inspect
import unittest

from src.core import contracts
from src.core.contracts import (
    AuditSink,
    CapabilityContract,
    CapabilityExecutionError,
    CapabilityExecutionRequest,
    CapabilityExecutionResult,
    EventSink,
    ExecutorPort,
    StoragePort,
)


class CoreContractShapeTest(unittest.TestCase):
    def test_capability_request_fields_are_generic(self) -> None:
        self.assertEqual(
            [field.name for field in dataclasses.fields(CapabilityExecutionRequest)],
            [
                "capability_id",
                "conversation_id",
                "task_id",
                "node_id",
                "input_payload",
                "context_refs",
                "dependency_outputs",
                "metadata",
            ],
        )

    def test_capability_result_fields_are_generic(self) -> None:
        self.assertEqual(
            [field.name for field in dataclasses.fields(CapabilityExecutionResult)],
            [
                "capability_id",
                "task_id",
                "node_id",
                "output_payload",
                "artifacts",
                "events",
                "interrupt",
                "error",
                "metadata",
            ],
        )

    def test_capability_error_fields_are_generic(self) -> None:
        self.assertEqual(
            [field.name for field in dataclasses.fields(CapabilityExecutionError)],
            ["code", "message", "retriable", "metadata"],
        )

    def test_storage_port_exposes_only_shared_model_operations(self) -> None:
        self.assertEqual(
            sorted(StoragePort.__dict__.get("__annotations__", {}).keys()),
            [],
        )
        for method_name in [
            "save_conversation",
            "get_conversation",
            "save_message",
            "save_task",
            "get_task",
            "get_active_task_for_conversation",
            "list_tasks_for_conversation",
            "save_task_node",
            "get_task_node",
            "list_task_nodes_for_task",
            "save_artifact",
            "append_event",
            "list_events_for_task",
            "list_events_for_task_filtered",
            "list_event_page_for_task",
            "save_mailbox_message",
            "get_mailbox_message",
            "save_mailbox_delivery",
            "get_mailbox_delivery",
            "list_mailbox_messages_for_task",
            "list_mailbox_deliveries_for_message",
            "save_interrupt",
            "get_interrupt",
            "get_interrupt_for_node",
            "list_interrupts_for_task",
            "save_interrupt_answer",
            "get_interrupt_answer",
            "list_interrupt_answers",
            "save_checkpoint",
            "get_checkpoint",
            "get_checkpoint_by_resume_token",
            "list_checkpoints_for_task",
        ]:
            self.assertTrue(callable(getattr(StoragePort, method_name)))

    def test_capability_contract_and_executor_ports_are_async(self) -> None:
        self.assertTrue(callable(getattr(CapabilityContract, "execute")))
        self.assertTrue(callable(getattr(ExecutorPort, "execute")))
        self.assertTrue(callable(getattr(ExecutorPort, "supports")))
        self.assertTrue(inspect.iscoroutinefunction(CapabilityContract.execute))
        self.assertTrue(inspect.iscoroutinefunction(ExecutorPort.execute))

    def test_event_and_audit_sinks_are_async(self) -> None:
        self.assertTrue(inspect.iscoroutinefunction(EventSink.publish))
        self.assertTrue(inspect.iscoroutinefunction(AuditSink.record))


class CoreContractBoundaryTest(unittest.TestCase):
    def test_core_contracts_do_not_reference_capability_specific_terms(self) -> None:
        source = inspect.getsource(contracts).lower()
        for forbidden in ("generic_data_lookup", "business_rule_engine", "domain_context_builder"):
            self.assertNotIn(forbidden, source)

    def test_core_contracts_do_not_import_api_or_storage_frameworks(self) -> None:
        source = inspect.getsource(contracts).lower()
        for forbidden in ("fastapi", "sqlalchemy"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

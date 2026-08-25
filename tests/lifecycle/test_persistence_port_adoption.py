from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from src.core import contracts
from src.lifecycle import (
    cancellation_service,
    conversation_guard,
    interrupt_service,
    mailbox_service,
    mcp_presence,
)


EXPECTED_COMPOSITE_BASES = {
    cancellation_service.CancellationLifecycleStoragePort: (
        contracts.TaskStoragePort,
        contracts.InterruptStoragePort,
        contracts.MailboxStoragePort,
        contracts.CheckpointStoragePort,
        contracts.EventStoragePort,
    ),
    interrupt_service.InterruptLifecycleStoragePort: (
        contracts.TaskStoragePort,
        contracts.InterruptStoragePort,
        contracts.EventStoragePort,
        contracts.CheckpointStoragePort,
        contracts.MCPRemoteTaskStoragePort,
    ),
}


class LifecyclePersistencePortAdoptionTest(unittest.TestCase):
    def test_composite_ports_have_no_methods_and_exact_narrow_surface(self) -> None:
        for port, expected_bases in EXPECTED_COMPOSITE_BASES.items():
            direct_async = tuple(
                name
                for name, value in port.__dict__.items()
                if inspect.iscoroutinefunction(value)
            )
            self.assertEqual(direct_async, (), port.__name__)
            inherited = {
                name
                for name, value in inspect.getmembers(
                    port, predicate=inspect.iscoroutinefunction
                )
            }
            expected = {
                name
                for base in expected_bases
                for name, value in inspect.getmembers(
                    base, predicate=inspect.iscoroutinefunction
                )
            }
            self.assertEqual(inherited, expected, port.__name__)

    def test_lifecycle_storage_annotations_are_narrow(self) -> None:
        cases = (
            (cancellation_service.CancellationService, "CancellationLifecycleStoragePort"),
            (conversation_guard.ConversationSerialGuard, "TaskStoragePort"),
            (interrupt_service.InterruptService, "InterruptLifecycleStoragePort"),
            (mailbox_service.MailboxService, "MailboxStoragePort"),
            (mcp_presence.MCPTaskPresenceService, "MCPRemoteTaskStoragePort | None"),
        )
        for service, expected in cases:
            annotation = inspect.signature(service.__init__).parameters["storage"].annotation
            self.assertEqual(annotation, expected, service.__name__)

    def test_lifecycle_consumers_do_not_import_aggregate_storage_port(self) -> None:
        root = Path(__file__).resolve().parents[2]
        for relative_path in (
            "src/lifecycle/cancellation_service.py",
            "src/lifecycle/conversation_guard.py",
            "src/lifecycle/interrupt_service.py",
            "src/lifecycle/mailbox_service.py",
            "src/lifecycle/mcp_presence.py",
        ):
            tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
            imported = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and node.module == "src.core.contracts"
                for alias in node.names
            }
            self.assertNotIn("StoragePort", imported, relative_path)


if __name__ == "__main__":
    unittest.main()

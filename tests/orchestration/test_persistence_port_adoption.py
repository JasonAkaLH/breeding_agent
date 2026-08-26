from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from src.core import contracts
from src.capabilities.main_agent import skill_output_artifacts
from src.orchestration import conversation_memory, visible_message_history
from src.orchestration.agent_loop import task_projection
from src.storage.postgres.repositories import PostgreSQLStorage
from src.storage.sqlite.repositories import SQLiteStorage


EXPECTED_COMPOSITE_BASES = {
    "ConversationMemoryStoragePort": (
        contracts.ConversationStoragePort,
        contracts.MessageStoragePort,
        contracts.TaskStoragePort,
        contracts.ArtifactStoragePort,
        conversation_memory.ConversationMemorySummaryMaterializationPort,
    ),
    "SkillOutputArtifactStoragePort": (
        contracts.ArtifactStoragePort,
        contracts.TaskStoragePort,
    ),
    "AgentTaskProjectionStoragePort": (
        contracts.TaskStoragePort,
        contracts.InterruptStoragePort,
        contracts.ConversationStoragePort,
        contracts.MCPRemoteTaskStoragePort,
        contracts.SlotStoragePort,
    ),
}


class PersistencePortAdoptionTest(unittest.TestCase):
    def test_production_storages_adopt_exact_memory_materialization_port(self) -> None:
        port = conversation_memory.ConversationMemorySummaryMaterializationPort
        self.assertTrue(issubclass(SQLiteStorage, port))
        self.assertTrue(issubclass(PostgreSQLStorage, port))

    def test_p2_composite_ports_have_no_methods_and_exact_narrow_bases(self) -> None:
        modules = (
            conversation_memory,
            skill_output_artifacts,
            task_projection,
        )
        for port_name, expected_bases in EXPECTED_COMPOSITE_BASES.items():
            port = next(
                getattr(module, port_name)
                for module in modules
                if hasattr(module, port_name)
            )
            direct_async = tuple(
                name
                for name, value in port.__dict__.items()
                if inspect.iscoroutinefunction(value)
            )
            self.assertEqual(direct_async, (), port_name)
            self.assertTrue(all(issubclass(port, base) for base in expected_bases))
            inherited_names = {
                name
                for name, value in inspect.getmembers(
                    port, predicate=inspect.iscoroutinefunction
                )
            }
            expected_names = {
                name
                for base in expected_bases
                for name, value in inspect.getmembers(
                    base, predicate=inspect.iscoroutinefunction
                )
            }
            self.assertEqual(inherited_names, expected_names, port_name)

    def test_p2_storage_annotations_use_only_narrow_ports(self) -> None:
        self.assertEqual(
            inspect.signature(conversation_memory.ConversationMemoryBuilder.__init__)
            .parameters["storage"]
            .annotation,
            "ConversationMemoryStoragePort",
        )
        self.assertEqual(
            inspect.signature(skill_output_artifacts.SkillOutputArtifactManager.__init__)
            .parameters["storage"]
            .annotation,
            "SkillOutputArtifactStoragePort",
        )
        self.assertEqual(
            inspect.signature(task_projection.AgentTaskInvocationCommitPort.__init__)
            .parameters["storage"]
            .annotation,
            "AgentTaskProjectionStoragePort",
        )
        self.assertEqual(
            inspect.signature(task_projection.persist_agent_slot_interrupt_authority)
            .parameters["storage"]
            .annotation,
            "SlotStoragePort",
        )
        self.assertEqual(
            inspect.signature(visible_message_history.persist_interrupt_question_message)
            .parameters["storage"]
            .annotation,
            "MessageStoragePort",
        )

    def test_p2_owned_consumers_do_not_import_aggregate_storage_port(self) -> None:
        root = Path(__file__).resolve().parents[2]
        relative_paths = (
            "src/orchestration/conversation_memory.py",
            "src/orchestration/visible_message_history.py",
            "src/orchestration/agent_loop/task_projection.py",
            "src/capabilities/main_agent/skill_output_artifacts.py",
        )
        for relative_path in relative_paths:
            tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
            imported_names = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and node.module == "src.core.contracts"
                for alias in node.names
            }
            self.assertNotIn("StoragePort", imported_names, relative_path)


if __name__ == "__main__":
    unittest.main()

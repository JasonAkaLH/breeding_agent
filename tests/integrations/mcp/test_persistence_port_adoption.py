from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from src.core import contracts
from src.integrations.mcp import (
    audit,
    dispatch_coordinator,
    durable_result_lifecycle,
    gateway,
    recovery_worker,
    result_artifact_projection,
    selector_context,
)
from src.integrations.mcp.result_parsing import historical_reprojection


EXPECTED_COMPOSITE_BASES = {
    audit.MCPAuditStoragePort: (
        contracts.MCPRolloutStoragePort,
        contracts.ConversationStoragePort,
        contracts.TaskStoragePort,
    ),
    dispatch_coordinator.MCPDispatchCoordinatorStoragePort: (
        contracts.UserMCPConfigurationStoragePort,
        contracts.MCPDispatchStoragePort,
        contracts.MCPDispatchFinalizationStoragePort,
        contracts.MCPRemoteTaskStoragePort,
        contracts.ConversationStoragePort,
        contracts.TaskStoragePort,
        contracts.InterruptStoragePort,
        contracts.ArtifactStoragePort,
    ),
    durable_result_lifecycle.MCPDurableResultLifecycleStoragePort: (
        contracts.MCPResultLifecycleStoragePort,
        contracts.ArtifactStoragePort,
    ),
    gateway.MCPGatewayStoragePort: (
        contracts.UserMCPConfigurationStoragePort,
        contracts.ConversationStoragePort,
        contracts.TaskStoragePort,
        contracts.MCPRemoteTaskStoragePort,
    ),
    recovery_worker.MCPRemoteTaskStorage: (
        contracts.MCPRemoteTaskStoragePort,
        contracts.TaskStoragePort,
    ),
    result_artifact_projection.MCPResultArtifactProjectionStoragePort: (
        contracts.ArtifactStoragePort,
        contracts.MCPDispatchStoragePort,
        contracts.MCPDispatchFinalizationStoragePort,
        contracts.MCPResultLifecycleStoragePort,
    ),
    historical_reprojection.MCPHistoricalReprojectionStoragePort: (
        contracts.ArtifactStoragePort,
        contracts.MCPDispatchStoragePort,
        contracts.MCPDispatchFinalizationStoragePort,
        contracts.MCPResultLifecycleStoragePort,
    ),
    selector_context.MCPSelectorContextStoragePort: (
        contracts.UserMCPConfigurationStoragePort,
        contracts.MCPDispatchStoragePort,
        contracts.MCPDispatchFinalizationStoragePort,
        contracts.ArtifactStoragePort,
        contracts.MessageStoragePort,
        contracts.TaskStoragePort,
    ),
}


P3_STORAGE_CONSUMERS = (
    "src/integrations/mcp/audit.py",
    "src/integrations/mcp/cp7_safety.py",
    "src/integrations/mcp/cp7_terminal_lifecycle.py",
    "src/integrations/mcp/dispatch_coordinator.py",
    "src/integrations/mcp/durable_result_lifecycle.py",
    "src/integrations/mcp/gateway.py",
    "src/integrations/mcp/health.py",
    "src/integrations/mcp/legacy_migration_apply.py",
    "src/integrations/mcp/observability.py",
    "src/integrations/mcp/recovery_worker.py",
    "src/integrations/mcp/result_artifact_projection.py",
    "src/integrations/mcp/result_parsing/historical_reprojection.py",
    "src/integrations/mcp/selector_context.py",
    "src/integrations/mcp/shadow_compare.py",
    "src/integrations/mcp/user_client.py",
    "src/integrations/mcp/user_config.py",
)


class PersistencePortAdoptionTest(unittest.TestCase):
    def test_p3_composite_ports_reuse_only_narrow_contracts(self) -> None:
        for port, expected_bases in EXPECTED_COMPOSITE_BASES.items():
            direct_async = tuple(
                name
                for name, value in port.__dict__.items()
                if inspect.iscoroutinefunction(value)
            )
            self.assertEqual(direct_async, (), port.__name__)
            self.assertTrue(
                all(issubclass(port, base) for base in expected_bases),
                port.__name__,
            )
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
            self.assertEqual(inherited_names, expected_names, port.__name__)

    def test_p3_owned_consumers_do_not_import_aggregate_storage_port(self) -> None:
        root = Path(__file__).resolve().parents[3]
        for relative_path in P3_STORAGE_CONSUMERS:
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

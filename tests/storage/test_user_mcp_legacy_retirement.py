from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.core.enums import ConversationStatus, NodeStatus, RoutingMode, TaskStatus
from src.core.models import Conversation, MCPLegacyRetirementEvidence, Task, TaskNode
from src.integrations.mcp.cp7_artifacts import canonical_sha256
from src.storage.sqlite.bootstrap import bootstrap_sqlite_database
from src.storage.sqlite.models import EventRecordRow
from src.storage.sqlite.repositories import SQLiteStorage


class UserMCPLegacyRetirementTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite+pysqlite:///{Path(self.temp_dir.name) / 'state.db'}"
        )
        bootstrap_sqlite_database(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.storage = SQLiteStorage(self.sessions)
        self.at = datetime(2026, 8, 13, 3, 0)
        await self.storage.save_conversation(
            Conversation(
                conversation_id="conv-legacy",
                username="owner-a",
                status=ConversationStatus.ACTIVE,
                created_at=self.at,
                updated_at=self.at,
            )
        )

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    async def test_only_durable_inventory_evidence_triggers_retirement(self) -> None:
        await self.storage.save_task(
            Task(
                task_id="task-legacy",
                conversation_id="conv-legacy",
                root_message_id="message-legacy",
                status=TaskStatus.RUNNING,
                routing_mode=RoutingMode.AUTO,
                created_at=self.at,
                updated_at=self.at,
                mcp_execution_mode="legacy",
                mcp_shadow_enabled=False,
                mcp_rollout_config_version="legacy",
                mcp_route_reason_code="routing_off",
                mcp_rollout_mode="off",
            )
        )
        await self.storage.save_task_node(
            TaskNode(
                node_id="node-legacy",
                task_id="task-legacy",
                capability_id="legacy.mcp.tool",
                status=NodeStatus.RUNNING,
            )
        )
        digest = canonical_sha256({"inventory": 1})
        key = f"legacy-retire:v1:task-legacy:{digest}"
        self.assertEqual(
            str(
                await self.storage.converge_legacy_runtime_retirement(
                    "task-legacy", "inventory-1", digest, key, self.at
                )
            ),
            "not_applicable",
        )
        evidence = MCPLegacyRetirementEvidence(
            evidence_id="evidence-1",
            task_id="task-legacy",
            inventory_id="inventory-1",
            inventory_sha256=digest,
            bundle_revision="mcprev-000001-abcdefabcdef",
            capability_id="legacy.mcp.tool",
            may_have_dispatched=False,
            evidence_sha256=canonical_sha256({"evidence": 1}),
            created_at=self.at,
        )
        await self.storage.append_mcp_legacy_retirement_evidence(evidence)
        await self.storage.append_mcp_legacy_retirement_evidence(evidence)
        self.assertEqual(
            str(
                await self.storage.converge_legacy_runtime_retirement(
                    "task-legacy", "inventory-1", digest, key, self.at
                )
            ),
            "converged",
        )
        self.assertEqual(
            str(
                await self.storage.converge_legacy_runtime_retirement(
                    "task-legacy", "inventory-1", digest, key, self.at
                )
            ),
            "already_converged",
        )
        with self.sessions() as session:
            self.assertEqual(
                len(
                    session.scalars(
                        select(EventRecordRow).where(
                            EventRecordRow.task_id == "task-legacy"
                        )
                    ).all()
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()

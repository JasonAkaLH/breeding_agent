from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.orchestration.agent_loop.models import (
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
)
from src.orchestration.agent_loop.transient_results import (
    AGENT_TRANSIENT_SKILL_RESULT_MANIFEST_SCHEMA,
    AGENT_TRANSIENT_SKILL_RESULT_SOURCE_KIND,
    AgentTransientSkillResultStage,
    AgentTransientSkillResultResolver,
    AgentTransientSkillResultStore,
    transient_skill_result_stage_ref,
)
from src.orchestration.agent_loop.result_projection import (
    build_model_result_envelope,
)


class AgentTransientSkillResultStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "transient"
        self.store = AgentTransientSkillResultStore(self.root)
        self.run = AgentRun(
            run_id="run-1",
            task_id="task-1",
            conversation_id="conversation-1",
            status=AgentRunStatus.RUNNING,
            binding=AgentModelBinding("edition-a"),
        )
        self.call = AgentItem(
            item_id="call-1",
            run_id=self.run.run_id,
            task_id=self.run.task_id,
            sequence=2,
            kind=AgentItemKind.TOOL_CALL,
            state=AgentItemState.COMMITTED,
            payload_json=(
                '{"capability_id":"skill.lookup","node_id":"node-1"}\n'
            ),
            payload_sha256="a" * 64,
        )
        self.raw = b'{"records":[1,2,3]}\n'
        self.raw_sha256 = hashlib.sha256(self.raw).hexdigest()
        self.stage_ref = transient_skill_result_stage_ref(
            call_item_id=self.call.item_id,
            raw_sha256=self.raw_sha256,
            projection_revision="skill-result-v2",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def stage(self, **overrides: object) -> AgentTransientSkillResultStage:
        values = {
            "run": self.run,
            "call_item": self.call,
            "result_item_id": "result-1",
            "node_id": "node-1",
            "capability_id": "skill.lookup",
            "canonical_raw_bytes": self.raw,
            "raw_sha256": self.raw_sha256,
            "projection_revision": "skill-result-v2",
            "expected_stage_ref": self.stage_ref,
        }
        values.update(overrides)
        return self.store.stage(**values)

    def test_stage_is_private_exact_and_contains_no_artifact_or_location(self) -> None:
        first = self.stage()
        replay = self.stage()

        self.assertEqual(first, replay)
        self.assertEqual(first.stage_ref, self.stage_ref)
        self.assertEqual(first.raw_size_bytes, len(self.raw))
        self.assertEqual(first.raw_sha256, self.raw_sha256)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        manifest_paths = tuple(self.store.manifest_root.iterdir())
        self.assertEqual(len(manifest_paths), 1)
        raw_paths = tuple(
            path
            for path in self.store.raw_root.rglob("*")
            if path.is_file()
        )
        self.assertEqual(len(raw_paths), 1)
        self.assertEqual(stat.S_IMODE(manifest_paths[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(raw_paths[0].stat().st_mode), 0o600)
        self.assertEqual(raw_paths[0].read_bytes(), self.raw)

        manifest = json.loads(manifest_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(
            set(manifest),
            {
                "schema",
                "source_kind",
                "stage_ref",
                "run_id",
                "task_id",
                "conversation_id",
                "node_id",
                "call_item_id",
                "result_item_id",
                "capability_id",
                "raw_size_bytes",
                "raw_sha256",
                "projection_revision",
                "staged_at",
            },
        )
        self.assertEqual(
            manifest["schema"], AGENT_TRANSIENT_SKILL_RESULT_MANIFEST_SCHEMA
        )
        self.assertEqual(
            manifest["source_kind"], AGENT_TRANSIENT_SKILL_RESULT_SOURCE_KIND
        )
        serialized = json.dumps(manifest, sort_keys=True)
        for forbidden in (
            "artifact_id",
            "storage_key",
            "storage_ref",
            "path",
            "url",
            "preview",
            "records",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_identity_or_content_drift_fails_closed(self) -> None:
        self.stage()
        drifted_run = AgentRun(
            run_id=self.run.run_id,
            task_id=self.run.task_id,
            conversation_id="conversation-other",
            status=AgentRunStatus.RUNNING,
            binding=self.run.binding,
        )

        with self.assertRaisesRegex(
            ValueError, "agent_transient_skill_result_stage_conflict"
        ):
            self.stage(run=drifted_run)
        with self.assertRaisesRegex(
            ValueError, "agent_transient_skill_result_stage_identity_invalid"
        ):
            self.stage(raw_sha256="b" * 64)

    def test_symlink_manifest_is_rejected_without_path_disclosure(self) -> None:
        target = Path(self.temporary.name) / "outside.json"
        target.write_text("{}", encoding="utf-8")
        manifest_path = self.store.manifest_path(self.stage_ref)
        os.symlink(target, manifest_path)

        with self.assertRaises(ValueError) as captured:
            self.stage()

        self.assertNotIn(str(target), str(captured.exception))
        self.assertNotIn(str(manifest_path), str(captured.exception))

    def test_resolver_replaces_exact_receipt_with_complete_raw(self) -> None:
        self.stage()
        receipt = build_model_result_envelope(
            projection_revision="skill-result-v2",
            projection_mode="transient_staged",
            model_view={
                "complete_result_pending_context_injection": True,
                "schema": "maf.agent.transient_skill_result_receipt.v1",
                "stage_ref": self.stage_ref,
            },
            original_size_bytes=len(self.raw),
            raw_sha256=self.raw_sha256,
            projection_truncated=True,
        )
        durable_payload = {
            "artifact_refs": [],
            "call_item_id": self.call.item_id,
            "outcome": "completed",
            "safe_error_code": None,
            "safe_result": receipt,
        }
        result = AgentItem(
            item_id="result-1",
            run_id=self.run.run_id,
            task_id=self.run.task_id,
            sequence=3,
            kind=AgentItemKind.TOOL_RESULT,
            state=AgentItemState.COMMITTED,
            payload_json=json.dumps(durable_payload) + "\n",
            payload_sha256="b" * 64,
            source_call_item_id=self.call.item_id,
        )
        resolver = AgentTransientSkillResultResolver(self.store)

        first = resolver.resolve_tool_result(
            run=self.run,
            call_item=self.call,
            result_item=result,
            durable_payload=durable_payload,
        )
        replay = resolver.resolve_tool_result(
            run=self.run,
            call_item=self.call,
            result_item=result,
            durable_payload=durable_payload,
        )

        self.assertEqual(first, replay)
        self.assertEqual(
            first,
            {
                "artifact_refs": [],
                "outcome": "completed",
                "safe_error_code": None,
                "safe_result": {
                    "schema": "maf.agent.skill_result_full.v1",
                    "result": {"records": [1, 2, 3]},
                },
            },
        )
        serialized = json.dumps(first, sort_keys=True)
        self.assertNotIn("stage_ref", serialized)
        self.assertNotIn("pending_context_injection", serialized)

        unavailable = {
            **durable_payload,
            "safe_result": {**receipt, "raw_sha256": "c" * 64},
        }
        with self.assertRaisesRegex(
            ValueError, "agent_transient_skill_result_unavailable"
        ):
            resolver.resolve_tool_result(
                run=self.run,
                call_item=self.call,
                result_item=result,
                durable_payload=unavailable,
            )

        next(
            path for path in self.store.raw_root.rglob("*") if path.is_file()
        ).unlink()
        with self.assertRaisesRegex(
            ValueError, "agent_transient_skill_result_unavailable"
        ):
            resolver.resolve_tool_result(
                run=self.run,
                call_item=self.call,
                result_item=result,
                durable_payload=durable_payload,
            )

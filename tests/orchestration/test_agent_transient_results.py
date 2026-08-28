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
    AgentTransientSkillResultStore,
    transient_skill_result_stage_ref,
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
            payload_json="{}\n",
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

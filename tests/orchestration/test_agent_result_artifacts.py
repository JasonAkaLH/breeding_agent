from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.enums import ArtifactType, TaskStatus
from src.core.models import Artifact, Task
from src.orchestration.agent_loop.models import (
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
)
from src.orchestration.agent_loop.result_artifacts import (
    AgentSkillResultArtifactJanitor,
    AgentSkillResultArtifactResolver,
    AgentSkillResultArtifactStager,
    parse_skill_result_storage_ref,
)
from src.orchestration.agent_loop.result_projection import (
    SKILL_RESULT_PROJECTION_REVISION,
    SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_LEGACY,
    AgentCallResultProjector,
    skill_result_artifact_id,
)
from src.storage.artifact_files import LocalArtifactFileStore


class AgentSkillResultArtifactStagerTest(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.file_store = LocalArtifactFileStore(self.root / "artifacts")
        self.now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        self.stager = AgentSkillResultArtifactStager(
            file_store=self.file_store,
            manifest_root=self.root / "manifests",
            now_fn=lambda: self.now,
        )
        self.run = AgentRun(
            run_id="run-1",
            task_id="task-1",
            conversation_id="conv-1",
            status=AgentRunStatus.RUNNING,
            binding=AgentModelBinding("edition-a"),
        )
        self.call_item = AgentItem(
            item_id="call-item-1",
            run_id="run-1",
            task_id="task-1",
            sequence=2,
            kind=AgentItemKind.TOOL_CALL,
            state=AgentItemState.COMMITTED,
            payload_json="{}\n",
            payload_sha256="0" * 64,
        )
        self.raw = b'{"articles":[1,2,3]}\n'
        self.raw_sha = hashlib.sha256(self.raw).hexdigest()
        self.artifact_id = skill_result_artifact_id(
            call_item_id=self.call_item.item_id,
            raw_sha256=self.raw_sha,
            projection_revision=SKILL_RESULT_PROJECTION_REVISION,
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        super().tearDown()

    def stage(self):
        return self.stager.stage(
            run=self.run,
            call_item=self.call_item,
            node_id="node-1",
            canonical_raw_bytes=self.raw,
            raw_sha256=self.raw_sha,
            projection_revision=SKILL_RESULT_PROJECTION_REVISION,
            expected_artifact_id=self.artifact_id,
        )

    def test_stage_writes_private_deterministic_raw_and_separate_manifest(self) -> None:
        artifact = self.stage()

        self.assertEqual(artifact.artifact_id, self.artifact_id)
        metadata = parse_skill_result_storage_ref(artifact.storage_ref)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["source_kind"], "skill_result")
        self.assertEqual(metadata["task_id"], self.run.task_id)
        self.assertEqual(metadata["call_item_id"], self.call_item.item_id)
        self.assertNotIn(str(self.root), artifact.storage_ref)
        raw_path = self.file_store.open_verified_path(
            str(metadata["storage_key"]),
            expected_size_bytes=int(metadata["size_bytes"]),
            expected_sha256=str(metadata["sha256"]),
        )
        self.assertEqual(raw_path.read_bytes(), self.raw)
        self.assertEqual(stat.S_IMODE(raw_path.stat().st_mode), 0o600)
        manifests = list(self.stager.manifest_root.iterdir())
        self.assertEqual(len(manifests), 1)
        self.assertEqual(stat.S_IMODE(manifests[0].stat().st_mode), 0o600)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["staged_at"], self.now.isoformat())
        self.assertNotIn("username", manifest)
        self.assertNotIn("body", manifest)

    def test_exact_replay_preserves_first_staged_at_and_reuses_bytes(self) -> None:
        first = self.stage()
        self.now += timedelta(hours=1)

        replay = self.stage()

        self.assertEqual(replay, first)
        manifest_path = next(self.stager.manifest_root.iterdir())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["staged_at"],
            datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc).isoformat(),
        )

    def test_two_workers_reuse_one_manifest_and_one_raw_file(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.stage) for _ in range(2)]
            results = [future.result(timeout=5) for future in futures]

        self.assertEqual(results[0], results[1])
        self.assertEqual(len(list(self.stager.manifest_root.iterdir())), 1)
        metadata = parse_skill_result_storage_ref(results[0].storage_ref)
        raw_path = self.file_store.open_path(str(metadata["storage_key"]))
        self.assertEqual(raw_path.read_bytes(), self.raw)
        self.assertEqual(tuple(raw_path.parent.iterdir()), (raw_path,))

    def test_manifest_identity_drift_and_content_digest_lie_fail_closed(self) -> None:
        self.stage()
        manifest_path = next(self.stager.manifest_root.iterdir())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["task_id"] = "different-task"
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "manifest_conflict"):
            self.stage()

        manifest["task_id"] = self.run.task_id
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
        with self.assertRaisesRegex(FileExistsError, "content"):
            self.stager.stage(
                run=self.run,
                call_item=self.call_item,
                node_id="node-1",
                canonical_raw_bytes=b'{"articles":[3,2,1]}\n',
                raw_sha256=self.raw_sha,
                projection_revision=SKILL_RESULT_PROJECTION_REVISION,
                expected_artifact_id=self.artifact_id,
            )

    def test_file_stage_failure_leaves_only_private_manifest_and_no_artifact_authority(self) -> None:
        def fail_save_bytes(**_kwargs):
            raise OSError("disk full")

        self.file_store.save_bytes = fail_save_bytes  # type: ignore[method-assign]
        with self.assertRaisesRegex(OSError, "disk full"):
            self.stage()

        manifests = list(self.stager.manifest_root.iterdir())
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["artifact_id"], self.artifact_id)
        self.assertFalse(
            self.file_store.open_path(str(manifest["storage_key"])).exists()
        )


class AgentSkillResultArtifactResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.file_store = LocalArtifactFileStore(self.root / "artifacts")
        self.run = AgentRun(
            run_id="run-resolve",
            task_id="task-resolve",
            conversation_id="conv-resolve",
            status=AgentRunStatus.RUNNING,
            binding=AgentModelBinding("edition-a"),
        )
        call_payload = {
            "arguments_json": "{}",
            "call_id": "provider-resolve",
            "capability_id": "skill.lookup",
            "node_id": "node-resolve",
            "provider_safe_name": "skill_lookup",
        }
        call_text = json.dumps(
            call_payload, sort_keys=True, separators=(",", ":")
        ) + "\n"
        self.call = AgentItem(
            item_id="call-resolve",
            run_id=self.run.run_id,
            task_id=self.run.task_id,
            sequence=3,
            kind=AgentItemKind.TOOL_CALL,
            state=AgentItemState.COMMITTED,
            payload_json=call_text,
            payload_sha256=hashlib.sha256(call_text.encode()).hexdigest(),
        )
        self.raw_payload = {
            "records": ["BEGIN-ARTIFACT", "x" * 150_000, "END-ARTIFACT"]
        }
        self.projection = AgentCallResultProjector().project(
            capability_id="skill.lookup",
            output_payload=self.raw_payload,
            call_item_id=self.call.item_id,
            outcome="completed",
            safe_error_code=None,
            artifact_ids=("business-artifact",),
            skill_projection_policy=(
                SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_LEGACY
            ),
        )
        staged = AgentSkillResultArtifactStager(
            file_store=self.file_store,
            manifest_root=self.root / "manifests",
        ).stage(
            run=self.run,
            call_item=self.call,
            node_id="node-resolve",
            canonical_raw_bytes=self.projection.canonical_raw_bytes,
            raw_sha256=self.projection.raw_sha256,
            projection_revision=self.projection.projection_revision,
            expected_artifact_id=self.projection.spill_artifact_id,
        )
        self.artifact = Artifact(
            artifact_id=staged.artifact_id,
            task_id=self.run.task_id,
            producer_node_id="node-resolve",
            artifact_type=ArtifactType.FILE,
            storage_ref=staged.storage_ref,
            summary=staged.summary,
            is_complete=True,
        )
        self.durable_payload = {
            "artifact_refs": ["business-artifact", staged.artifact_id],
            "call_item_id": self.call.item_id,
            "outcome": "completed",
            "safe_error_code": None,
            "safe_result": self.projection.safe_result_payload,
        }
        result_text = json.dumps(
            self.durable_payload, sort_keys=True, separators=(",", ":")
        ) + "\n"
        self.result = AgentItem(
            item_id="result-resolve",
            run_id=self.run.run_id,
            task_id=self.run.task_id,
            sequence=4,
            kind=AgentItemKind.TOOL_RESULT,
            state=AgentItemState.COMMITTED,
            payload_json=result_text,
            payload_sha256=hashlib.sha256(result_text.encode()).hexdigest(),
            source_call_item_id=self.call.item_id,
        )
        self.resolver = AgentSkillResultArtifactResolver(self.file_store)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        super().tearDown()

    def test_resolve_returns_full_canonical_result_and_preserves_artifact_ids(
        self,
    ) -> None:
        resolved = self.resolver.resolve_tool_result(
            run=self.run,
            call_item=self.call,
            result_item=self.result,
            durable_payload=self.durable_payload,
            artifact=self.artifact,
        )

        self.assertEqual(
            resolved["artifact_refs"], self.durable_payload["artifact_refs"]
        )
        self.assertEqual(
            resolved["safe_result"],
            {
                "schema": "maf.agent.skill_result_full.v1",
                "result": self.raw_payload,
            },
        )

    def test_missing_incomplete_or_identity_drift_fails_closed(self) -> None:
        invalid = (
            None,
            replace(self.artifact, is_complete=False),
            replace(self.artifact, task_id="other-task"),
            replace(self.artifact, producer_node_id="other-node"),
            replace(self.artifact, artifact_id="other-artifact"),
        )
        for artifact in invalid:
            with self.subTest(artifact=artifact):
                with self.assertRaisesRegex(
                    ValueError, "agent_skill_result_artifact_unavailable"
                ):
                    self.resolver.resolve_tool_result(
                        run=self.run,
                        call_item=self.call,
                        result_item=self.result,
                        durable_payload=self.durable_payload,
                        artifact=artifact,
                    )

    def test_inactive_or_metadata_drift_fails_closed(self) -> None:
        for key, value in (
            ("retention_status", "inactive"),
            ("conversation_id", "other-conversation"),
            ("call_item_id", "other-call"),
            ("node_id", "other-node"),
            ("size_bytes", 1),
            ("sha256", "f" * 64),
        ):
            metadata = json.loads(self.artifact.storage_ref)
            metadata[key] = value
            drifted = replace(
                self.artifact,
                storage_ref=json.dumps(
                    metadata, sort_keys=True, separators=(",", ":")
                ),
            )
            with self.subTest(key=key):
                with self.assertRaisesRegex(
                    ValueError, "agent_skill_result_artifact_unavailable"
                ):
                    self.resolver.resolve_tool_result(
                        run=self.run,
                        call_item=self.call,
                        result_item=self.result,
                        durable_payload=self.durable_payload,
                        artifact=drifted,
                    )

    def test_file_content_or_mode_drift_fails_closed(self) -> None:
        metadata = parse_skill_result_storage_ref(self.artifact.storage_ref)
        assert metadata is not None
        raw_path = self.file_store.open_path(str(metadata["storage_key"]))
        raw_path.write_bytes(b'{"records":[]}\n')
        raw_path.chmod(0o600)
        with self.assertRaisesRegex(
            ValueError, "agent_skill_result_artifact_unavailable"
        ):
            self.resolver.resolve_tool_result(
                run=self.run,
                call_item=self.call,
                result_item=self.result,
                durable_payload=self.durable_payload,
                artifact=self.artifact,
            )

        raw_path.write_bytes(self.projection.canonical_raw_bytes)
        raw_path.chmod(0o644)
        with self.assertRaisesRegex(
            ValueError, "agent_skill_result_artifact_unavailable"
        ):
            self.resolver.resolve_tool_result(
                run=self.run,
                call_item=self.call,
                result_item=self.result,
                durable_payload=self.durable_payload,
                artifact=self.artifact,
            )


class AgentSkillResultArtifactJanitorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.file_store = LocalArtifactFileStore(self.root / "artifacts")
        self.now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        self.run = AgentRun(
            "run-janitor",
            "task-janitor",
            "conv-janitor",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-a"),
        )
        self.call = AgentItem(
            "call-janitor",
            self.run.run_id,
            self.run.task_id,
            1,
            AgentItemKind.TOOL_CALL,
            AgentItemState.COMMITTED,
            "{}\n",
            "0" * 64,
        )
        self.result = AgentItem(
            "result-janitor",
            self.run.run_id,
            self.run.task_id,
            2,
            AgentItemKind.TOOL_RESULT,
            AgentItemState.RESERVED,
            "{}\n",
            "1" * 64,
            source_call_item_id=self.call.item_id,
        )
        raw = b'{"large":true}\n'
        raw_sha = hashlib.sha256(raw).hexdigest()
        artifact_id = skill_result_artifact_id(
            call_item_id=self.call.item_id,
            raw_sha256=raw_sha,
            projection_revision=SKILL_RESULT_PROJECTION_REVISION,
        )
        staged = AgentSkillResultArtifactStager(
            file_store=self.file_store,
            manifest_root=self.root / "manifests",
            now_fn=lambda: self.now - timedelta(days=2),
        ).stage(
            run=self.run,
            call_item=self.call,
            node_id="node-janitor",
            canonical_raw_bytes=raw,
            raw_sha256=raw_sha,
            projection_revision=SKILL_RESULT_PROJECTION_REVISION,
            expected_artifact_id=artifact_id,
        )
        self.staged = staged
        self.manifest = next((self.root / "manifests").iterdir())
        old = (self.now - timedelta(days=2)).timestamp()
        os.utime(self.manifest, (old, old))

        class Storage:
            artifact = None
            task = Task(
                "task-janitor",
                "conv-janitor",
                "message-janitor",
                status=TaskStatus.RUNNING,
            )

            async def get_artifact(inner_self, _artifact_id):
                return inner_self.artifact

            async def get_task(inner_self, _task_id):
                return inner_self.task

        class Runs:
            run = self.run
            items = (self.call, self.result)

            async def get_run_for_task(inner_self, _task_id):
                return inner_self.run

            async def list_items(inner_self, _run_id):
                return inner_self.items

        self.storage = Storage()
        self.runs = Runs()
        self.janitor = AgentSkillResultArtifactJanitor(
            file_store=self.file_store,
            manifest_root=self.root / "manifests",
            storage=self.storage,
            runs=self.runs,
            now_fn=lambda: self.now,
        )

    async def asyncTearDown(self) -> None:
        self.tmpdir.cleanup()
        await super().asyncTearDown()

    async def test_registered_artifact_removes_manifest_but_preserves_raw(self) -> None:
        metadata = parse_skill_result_storage_ref(self.staged.storage_ref)
        raw_path = self.file_store.open_path(str(metadata["storage_key"]))
        self.storage.artifact = Artifact(
            self.staged.artifact_id,
            self.run.task_id,
            producer_node_id="node-janitor",
            artifact_type=ArtifactType.FILE,
            storage_ref=self.staged.storage_ref,
        )

        result = await self.janitor.run_once()

        self.assertEqual(result.manifests_removed, 1)
        self.assertEqual(result.raw_files_removed, 0)
        self.assertTrue(raw_path.exists())
        self.assertFalse(self.manifest.exists())

    async def test_reserved_recoverable_and_nonterminal_orphans_are_retained(self) -> None:
        for run, task, items in (
            (self.run, self.storage.task, (self.call, self.result)),
            (
                AgentRun(
                    self.run.run_id,
                    self.run.task_id,
                    self.run.conversation_id,
                    AgentRunStatus.FAILED,
                    self.run.binding,
                ),
                Task(
                    self.run.task_id,
                    self.run.conversation_id,
                    "message-janitor",
                    status=TaskStatus.FAILED,
                ),
                (self.call, self.result),
            ),
            (
                AgentRun(
                    self.run.run_id,
                    self.run.task_id,
                    self.run.conversation_id,
                    AgentRunStatus.FAILED,
                    self.run.binding,
                ),
                self.storage.task,
                (
                    self.call,
                    AgentItem(
                        self.result.item_id,
                        self.result.run_id,
                        self.result.task_id,
                        self.result.sequence,
                        self.result.kind,
                        AgentItemState.COMMITTED,
                        self.result.payload_json,
                        self.result.payload_sha256,
                        source_call_item_id=self.call.item_id,
                    ),
                ),
            ),
        ):
            with self.subTest(run_status=str(run.status), task_status=str(task.status)):
                self.runs.run = run
                self.runs.items = items
                self.storage.task = task
                result = await self.janitor.run_once()
                self.assertEqual(result.retained, 1)
                self.assertTrue(self.manifest.exists())

    async def test_closed_old_orphan_removes_manifest_and_raw(self) -> None:
        self.runs.run = AgentRun(
            self.run.run_id,
            self.run.task_id,
            self.run.conversation_id,
            AgentRunStatus.FAILED,
            self.run.binding,
        )
        self.runs.items = (
            self.call,
            AgentItem(
                self.result.item_id,
                self.result.run_id,
                self.result.task_id,
                self.result.sequence,
                self.result.kind,
                AgentItemState.COMMITTED,
                self.result.payload_json,
                self.result.payload_sha256,
                source_call_item_id=self.call.item_id,
            ),
        )
        self.storage.task = Task(
            self.run.task_id,
            self.run.conversation_id,
            "message-janitor",
            status=TaskStatus.FAILED,
        )
        metadata = parse_skill_result_storage_ref(self.staged.storage_ref)
        raw_path = self.file_store.open_path(str(metadata["storage_key"]))

        current = self.now.timestamp()
        os.utime(self.manifest, (current, current))
        too_new = await self.janitor.run_once()
        self.assertEqual(too_new.retained, 1)
        self.assertTrue(raw_path.exists())
        old = (self.now - timedelta(days=2)).timestamp()
        os.utime(self.manifest, (old, old))

        result = await self.janitor.run_once()

        self.assertEqual(result.manifests_removed, 1)
        self.assertEqual(result.raw_files_removed, 1)
        self.assertFalse(raw_path.exists())
        self.assertFalse(self.manifest.exists())

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.integrations.mcp.result_parsing import (
    MCPIsolatedResultService,
    MCPProjectionStore,
    MCPResultDecodeRequest,
    MCPResultSource,
)
from src.integrations.mcp.result_parsing.projection_store import (
    MCPProjectionBinding,
    MCPProjectionStoreError,
)
from src.integrations.mcp.result_parsing.service import (
    MAX_MAPPING_INPUT_BYTES,
    MAX_OWNER_JOBS,
    MAX_QUEUE_WAIT_SECONDS,
    MAX_QUEUED_JOBS,
    MCPResultWorkerError,
    MCPResultWorkerGate,
)
from src.integrations.mcp.result_parsing.worker import PARSER_REVISION
from src.integrations.mcp.result_parsing.worker import (
    MAX_WORKER_ADDRESS_SPACE_BYTES,
    _apply_worker_limits,
    _streaming_canonical_sha256,
)
from src.integrations.mcp.result_parsing.models import MCPRawResultDescriptor
from src.integrations.mcp.temporary_results import MCPTemporaryResultStore


def _rlimit_probe(connection) -> None:
    import resource

    _apply_worker_limits()
    connection.send(resource.getrlimit(resource.RLIMIT_AS))
    connection.close()


class MCPResultParserWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.projection_store = MCPProjectionStore(root / "projections")
        self.service = MCPIsolatedResultService(
            projection_store=self.projection_store
        )

    async def test_streaming_model_digest_matches_canonical_json_with_chunk_boundaries(self) -> None:
        value = {
            "float": -0.25,
            "nested": [True, None, {"quoted": ('界\\"\n' * 2_000)}],
        }
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self.assertEqual(
            _streaming_canonical_sha256(value),
            "sha256:" + hashlib.sha256(canonical).hexdigest(),
        )
    async def test_mapping_is_parsed_in_spawn_worker_and_projection_is_staged_then_published(self) -> None:
        self.assertEqual(PARSER_REVISION, "mcp-result-parser.v2")
        request = MCPResultDecodeRequest(
            protocol_version="2025-11-25",
            source=MCPResultSource.TOOLS_CALL,
            payload={
                "content": [{"type": "text", "text": "business result"}],
                "structuredContent": {"answer": 42},
            },
        )

        outcome = await self.service.parse(
            owner_user_id="alice",
            task_id="task-1",
            node_id="node-1",
            call_ref="call-1",
            request=request,
            measured_mapping_bytes=128,
        )

        self.assertEqual(outcome.checkpoint.outcome, "succeeded")
        self.assertEqual(outcome.checkpoint.call_ref, "call-1")
        self.assertRegex(outcome.checkpoint.checkpoint_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertIsNotNone(outcome.projection_staging_handle)
        handle = outcome.projection_staging_handle
        self.assertTrue(Path(handle.path).name.startswith(".staged-"))
        with self.assertRaises(MCPProjectionStoreError):
            self.projection_store.load(
                handle.token,
                binding=handle.binding,
                expected_projection_sha256=handle.projection_sha256,
            )

        published = self.projection_store.publish(handle)
        self.assertEqual(self.projection_store.publish(handle), published)
        envelope = self.projection_store.load(
            published.projection_ref,
            binding=handle.binding,
            expected_projection_sha256=published.projection_sha256,
        )

        self.assertEqual(envelope["user_view"]["primary"]["value"], {"answer": 42})
        self.assertIn("business result", envelope["agent_projection"])
        self.assertEqual(envelope["schema"], "maf.mcp.parsed_result_projection.v2")
        self.assertIs(envelope["agent_projection_truncated"], False)
        self.assertNotIn("protocol_version", json.dumps(envelope))

    async def test_parser_observation_is_closed_and_contains_no_business_text_or_identity(self) -> None:
        observations = []
        self.service.configure_observer(observations.append)

        await self.service.parse(
            owner_user_id="sensitive-owner",
            task_id="sensitive-task",
            node_id="sensitive-node",
            call_ref="sensitive-call",
            request=MCPResultDecodeRequest(
                protocol_version="2025-11-25",
                source=MCPResultSource.TOOLS_CALL,
                payload={
                    "content": [{"type": "text", "text": "secret body"}],
                    "structuredContent": {"answer": 42},
                },
            ),
            measured_mapping_bytes=128,
        )

        self.assertEqual(len(observations), 1)
        observation = observations[0]
        self.assertEqual(observation.outcome, "succeeded")
        self.assertEqual(observation.primary_kind, "structured")
        self.assertTrue(observation.structured_present)
        rendered = repr(observation)
        for forbidden in (
            "secret body",
            "sensitive-owner",
            "sensitive-task",
            "sensitive-node",
            "sensitive-call",
        ):
            self.assertNotIn(forbidden, rendered)

    async def test_malformed_and_tool_error_checkpoints_do_not_stage_success_projection(self) -> None:
        malformed = await self.service.parse(
            owner_user_id="alice",
            task_id="task-1",
            node_id="node-1",
            call_ref="call-malformed",
            request=MCPResultDecodeRequest(
                protocol_version="2025-11-25",
                source=MCPResultSource.TOOLS_CALL,
                payload={"content": "invalid"},
            ),
            measured_mapping_bytes=32,
        )
        self.assertEqual(malformed.checkpoint.outcome, "malformed")
        self.assertEqual(malformed.checkpoint.reason, "result_shape_invalid")
        self.assertIsNone(malformed.projection_staging_handle)

        tool_error = await self.service.parse(
            owner_user_id="alice",
            task_id="task-1",
            node_id="node-1",
            call_ref="call-error",
            request=MCPResultDecodeRequest(
                protocol_version="2025-11-25",
                source=MCPResultSource.TOOLS_CALL,
                payload={"content": [], "isError": True},
            ),
            measured_mapping_bytes=32,
        )
        self.assertEqual(tool_error.checkpoint.outcome, "tool_error")
        self.assertIsNone(tool_error.projection_staging_handle)

    async def test_projection_store_failure_after_checkpoint_keeps_succeeded_outcome(self) -> None:
        with patch.object(
            self.projection_store,
            "stage",
            side_effect=MCPProjectionStoreError("injected"),
        ):
            outcome = await self.service.parse(
                owner_user_id="alice",
                task_id="task-1",
                node_id="node-1",
                call_ref="call-projection-failure",
                request=MCPResultDecodeRequest(
                    protocol_version="2025-11-25",
                    source=MCPResultSource.TOOLS_CALL,
                    payload={"content": [], "structuredContent": {"ok": True}},
                ),
                measured_mapping_bytes=64,
            )

        self.assertEqual(outcome.checkpoint.outcome, "succeeded")
        self.assertIsNone(outcome.projection_staging_handle)
        self.assertEqual(outcome.projection_error, "projection_failed")

    async def test_mapping_requires_transport_length_evidence_at_64_kib_boundary(self) -> None:
        request = MCPResultDecodeRequest(
            protocol_version="2025-11-25",
            source=MCPResultSource.TOOLS_CALL,
            payload={"content": []},
        )
        for evidence in (None, MAX_MAPPING_INPUT_BYTES + 1, True):
            with self.subTest(evidence=evidence):
                with self.assertRaisesRegex(
                    MCPResultWorkerError, "mapping_length_evidence_invalid"
                ):
                    await self.service.parse(
                        owner_user_id="alice",
                        task_id="task-1",
                        node_id="node-1",
                        call_ref="call-boundary",
                        request=request,
                        measured_mapping_bytes=evidence,
                    )
        accepted = await self.service.parse(
            owner_user_id="alice",
            task_id="task-1",
            node_id="node-1",
            call_ref="call-boundary",
            request=request,
            measured_mapping_bytes=MAX_MAPPING_INPUT_BYTES,
        )
        self.assertEqual(accepted.checkpoint.outcome, "succeeded")

    async def test_identity_bound_raw_descriptor_matches_mapping_and_tamper_fails(self) -> None:
        raw_store = MCPTemporaryResultStore(
            Path(self.temporary.name) / "raw", memory_threshold_bytes=1
        )
        sink = raw_store.create_sink(
            "task-raw",
            durable=True,
            owner_user_id="alice",
            node_id="node-1",
            call_ref="call-raw",
        )
        raw = b'{"content":[],"structuredContent":{"answer":7}}'
        await sink.write(raw)
        ref = await sink.finalize()
        descriptor = raw_store.result_parser_descriptor(ref)
        outcome = await self.service.parse(
            owner_user_id="alice",
            task_id="task-raw",
            node_id="node-1",
            call_ref="call-raw",
            request=MCPResultDecodeRequest(
                protocol_version="2025-11-25",
                source=MCPResultSource.TOOLS_CALL,
                payload=descriptor,
            ),
        )
        self.assertEqual(outcome.checkpoint.outcome, "succeeded")

        os.chmod(descriptor.path, 0o640)
        with self.assertRaisesRegex(MCPResultWorkerError, "worker_failed"):
            await self.service.parse(
                owner_user_id="alice",
                task_id="task-raw",
                node_id="node-1",
                call_ref="call-raw-tampered",
                request=MCPResultDecodeRequest(
                    protocol_version="2025-11-25",
                    source=MCPResultSource.TOOLS_CALL,
                    payload=descriptor,
                ),
            )

    async def test_worker_timeout_is_terminable_and_event_loop_keeps_heartbeat(self) -> None:
        service = MCPIsolatedResultService(
            projection_store=self.projection_store,
            worker_timeout_seconds=0.001,
        )
        ticks = 0
        stop = asyncio.Event()

        async def heartbeat() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0)

        ticker = asyncio.create_task(heartbeat())
        try:
            with self.assertRaisesRegex(MCPResultWorkerError, "worker_timeout"):
                await service.parse(
                    owner_user_id="alice",
                    task_id="task-1",
                    node_id="node-1",
                    call_ref="call-timeout",
                    request=MCPResultDecodeRequest(
                        protocol_version="2025-11-25",
                        source=MCPResultSource.TOOLS_CALL,
                        payload={"content": []},
                    ),
                    measured_mapping_bytes=16,
                )
        finally:
            stop.set()
            await ticker
        self.assertGreater(ticks, 0)

    async def test_cancellation_terminates_current_job_and_releases_gate(self) -> None:
        service = MCPIsolatedResultService(
            projection_store=self.projection_store,
            worker_timeout_seconds=10,
        )
        task = asyncio.create_task(
            service.parse(
                owner_user_id="alice",
                task_id="task-1",
                node_id="node-1",
                call_ref="call-cancel",
                request=MCPResultDecodeRequest(
                    protocol_version="2025-11-25",
                    source=MCPResultSource.TOOLS_CALL,
                    payload={
                        "content": [],
                        "structuredContent": {"large": "x" * 60_000},
                    },
                ),
                measured_mapping_bytes=MAX_MAPPING_INPUT_BYTES,
            )
        )
        await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(service.gate.active, 0)
        self.assertEqual(service.gate.queued, 0)

    async def test_gate_enforces_one_active_two_per_owner_and_eight_queued(self) -> None:
        self.assertEqual(MAX_OWNER_JOBS, 2)
        self.assertEqual(MAX_QUEUED_JOBS, 8)
        self.assertEqual(MAX_QUEUE_WAIT_SECONDS, 30.0)
        gate = MCPResultWorkerGate()
        release = asyncio.Event()
        active = asyncio.Event()

        async def hold(owner: str, entered: asyncio.Event | None = None) -> None:
            async with gate.acquire(owner):
                if entered is not None:
                    entered.set()
                await release.wait()

        active_task = asyncio.create_task(hold("owner-0", active))
        await active.wait()
        owner_queued = asyncio.create_task(hold("owner-0"))
        await asyncio.sleep(0)
        with self.assertRaisesRegex(MCPResultWorkerError, "owner_capacity"):
            async with gate.acquire("owner-0"):
                self.fail("third owner job must not enter")

        queued = [
            asyncio.create_task(hold(f"owner-{index}")) for index in range(1, 8)
        ]
        await asyncio.sleep(0)
        self.assertEqual(gate.queued, 8)
        with self.assertRaisesRegex(MCPResultWorkerError, "queue_capacity"):
            async with gate.acquire("owner-overflow"):
                self.fail("ninth queued job must not enter")

        for task in [owner_queued, *queued]:
            task.cancel()
        await asyncio.gather(owner_queued, *queued, return_exceptions=True)
        release.set()
        await active_task
        self.assertEqual(gate.active, 0)
        self.assertEqual(gate.queued, 0)

    async def test_projection_store_rejects_tamper_and_cleans_only_old_staged_files(self) -> None:
        binding = MCPProjectionBinding(
            owner_user_id="alice",
            task_id="task",
            node_id="node",
            call_ref="call",
            raw_sha256="sha256:" + "a" * 64,
            output_schema_sha256=None,
            source="tools_call",
            parser_revision=PARSER_REVISION,
        )
        envelope = json.dumps(
            {
                "schema": "maf.mcp.parsed_result_projection.v2",
                "parsed_model_sha256": "sha256:" + "b" * 64,
                "user_view": {},
                "agent_projection": "safe",
                "agent_projection_truncated": False,
                "workflow_control": None,
            },
            separators=(",", ":"),
        ).encode()
        fresh = self.projection_store.stage(envelope, binding=binding)
        old = self.projection_store.stage(envelope, binding=binding)
        os.utime(old.path, (0, 0), follow_symlinks=False)

        self.assertEqual(
            self.projection_store.cleanup_staged(older_than_seconds=1), 1
        )
        self.assertTrue(Path(fresh.path).exists())
        self.assertFalse(Path(old.path).exists())

        Path(fresh.path).write_bytes(b'{"schema":"tampered"}')
        os.chmod(fresh.path, 0o600)
        with self.assertRaises(MCPProjectionStoreError):
            self.projection_store.publish(fresh)

    async def test_projection_store_rejects_non_v2_and_shape_drift(self) -> None:
        binding = MCPProjectionBinding(
            owner_user_id="alice",
            task_id="task",
            node_id="node",
            call_ref="call",
            raw_sha256="sha256:" + "a" * 64,
            output_schema_sha256=None,
            source="tools_call",
            parser_revision=PARSER_REVISION,
        )
        base = {
            "schema": "maf.mcp.parsed_result_projection.v2",
            "parsed_model_sha256": "sha256:" + "b" * 64,
            "user_view": {},
            "agent_projection": "safe",
            "agent_projection_truncated": False,
            "workflow_control": None,
        }
        invalid = []
        for schema in (
            "maf.mcp.parsed_result_projection.v1",
            "maf.mcp.parsed_result_projection.v3",
        ):
            invalid.append({**base, "schema": schema})
        invalid.extend(
            (
                {key: value for key, value in base.items() if key != "agent_projection_truncated"},
                {**base, "unknown": True},
                {**base, "agent_projection_truncated": 0},
            )
        )

        for envelope in invalid:
            with self.subTest(envelope=envelope):
                with self.assertRaises(MCPProjectionStoreError):
                    self.projection_store.stage(
                        json.dumps(envelope, separators=(",", ":")).encode(),
                        binding=binding,
                    )
        with self.assertRaises(MCPProjectionStoreError):
            self.projection_store.stage(
                json.dumps(base, separators=(",", ":")).encode(),
                binding=MCPProjectionBinding(
                    owner_user_id=binding.owner_user_id,
                    task_id=binding.task_id,
                    node_id=binding.node_id,
                    call_ref=binding.call_ref,
                    raw_sha256=binding.raw_sha256,
                    output_schema_sha256=binding.output_schema_sha256,
                    source=binding.source,
                    parser_revision="mcp-result-parser.v1",
                ),
            )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux RLIMIT gate")
    async def test_linux_worker_enforces_512_mib_and_parses_64_mib_boundary(self) -> None:
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=_rlimit_probe, args=(send,))
        process.start()
        send.close()
        limits = await asyncio.to_thread(receive.recv)
        await asyncio.to_thread(process.join, 5)
        receive.close()
        self.assertEqual(
            limits,
            (MAX_WORKER_ADDRESS_SPACE_BYTES, MAX_WORKER_ADDRESS_SPACE_BYTES),
        )

        prefix = b'{"content":[{"type":"text","text":"'
        suffix = b'"}]}'
        raw = prefix + b"x" * (64 * 1024 * 1024 - len(prefix) - len(suffix)) + suffix
        path = Path(self.temporary.name) / "raw-64m.json"
        path.write_bytes(raw)
        os.chmod(path, 0o600)
        metadata = os.stat(path, follow_symlinks=False)
        descriptor = MCPRawResultDescriptor(
            path=str(path),
            size_bytes=len(raw),
            sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
            device=metadata.st_dev,
            inode=metadata.st_ino,
            owner_uid=metadata.st_uid,
        )
        try:
            outcome = await self.service.parse(
                owner_user_id="alice",
                task_id="task-64m",
                node_id="node-64m",
                call_ref="call-64m",
                request=MCPResultDecodeRequest(
                    protocol_version="2025-11-25",
                    source=MCPResultSource.TOOLS_CALL,
                    payload=descriptor,
                ),
            )
        except MCPResultWorkerError as exc:
            self.fail(f"64 MiB worker failed: {exc.worker_category}")
        self.assertEqual(outcome.checkpoint.outcome, "succeeded")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux terminable regex gate")
    async def test_linux_malicious_schema_regex_is_terminated_by_wall_timeout(self) -> None:
        schema = {
            "type": "object",
            "properties": {"value": {"type": "string", "pattern": "^(a+)+$"}},
        }
        encoded = json.dumps(
            schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        service = MCPIsolatedResultService(
            projection_store=self.projection_store,
            worker_timeout_seconds=0.2,
        )
        with self.assertRaisesRegex(MCPResultWorkerError, "worker_timeout"):
            await service.parse(
                owner_user_id="alice",
                task_id="task-regex",
                node_id="node-regex",
                call_ref="call-regex",
                request=MCPResultDecodeRequest(
                    protocol_version="2025-11-25",
                    source=MCPResultSource.TOOLS_CALL,
                    payload={
                        "content": [],
                        "structuredContent": {"value": "a" * 30 + "!"},
                    },
                    output_schema=schema,
                    output_schema_sha256="sha256:" + hashlib.sha256(encoded).hexdigest(),
                ),
                measured_mapping_bytes=256,
            )


if __name__ == "__main__":
    unittest.main()

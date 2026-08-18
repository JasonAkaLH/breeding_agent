from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from src.core.models import (
    MCPCP7CandidateGuard,
    MCPCP7ReadyEpochEvent,
    MCPCP7SafetyLedgerRecord,
    MCPDispatchResumeOutbox,
    MCPExecutionTerminalProjection,
    MCPTerminalErrorCode,
    MCPNoServerIntent,
    MCPNoServerIntentStatus,
    MCPNoServerIntentTrigger,
    MCPTerminalResultReceipt,
    MCPValidatedTerminalResultCandidate,
    MCPUnavailableEventType,
    UserMCPOwnerMutationGuard,
)
from src.integrations.mcp.cp7_artifacts import (
    CP7ArtifactConflictError,
    CP7ArtifactValidationError,
    canonical_envelope_bytes,
    canonical_json_bytes,
    canonical_sha256,
    cp7_restore_id,
    mcp_dispatch_resume_outbox_id,
    mcp_no_server_intent_id,
    mcp_terminal_candidate_id,
    mcp_terminal_projection_id,
    mcp_terminal_receipt_id,
    parse_canonical_envelope_bytes,
    parse_canonical_json_bytes,
    publish_immutable,
    publish_or_compare_immutable,
    secure_read,
    secure_read_canonical_envelope,
)
from src.integrations.mcp.rollout import MCPRouteReason


class CP7CanonicalJSONTests(unittest.TestCase):
    def test_canonical_bytes_and_digest_are_stable(self) -> None:
        value = {"z": [True, None, "雪"], "a": {"b": 2, "a": 1}}

        encoded = canonical_json_bytes(value)

        self.assertEqual(encoded, b'{"a":{"a":1,"b":2},"z":[true,null,"\xe9\x9b\xaa"]}\n')
        self.assertEqual(
            canonical_sha256(value),
            "sha256:86193840513cb7d858dd7813df579b9c9c28434b6cc7fee270f21de174ed5f80",
        )
        self.assertEqual(parse_canonical_json_bytes(encoded), value)

    def test_parser_rejects_duplicate_noncanonical_and_nonfinite_json(self) -> None:
        hostile = (
            b'{"a":1,"a":1}\n',
            b'{"b":2, "a":1}\n',
            b'{"a":1}',
            b'{"a":NaN}\n',
            b'{"a":-0.0}\n',
        )

        for raw in hostile:
            with self.subTest(raw=raw):
                with self.assertRaises(CP7ArtifactValidationError):
                    parse_canonical_json_bytes(raw)

    def test_envelope_digest_excludes_schema_and_digest_field(self) -> None:
        payload = {"candidate": "C_A", "passed": True}

        encoded = canonical_envelope_bytes("maf.test.v1", payload)
        parsed = parse_canonical_json_bytes(encoded)

        self.assertEqual(set(parsed), {"schema", "payload", "payload_sha256"})
        self.assertEqual(parsed["payload_sha256"], canonical_sha256(payload))
        self.assertEqual(
            parse_canonical_envelope_bytes(encoded, expected_schema="maf.test.v1"),
            payload,
        )

    def test_envelope_reader_rejects_unknown_key_and_digest_drift(self) -> None:
        for envelope in (
            {
                "schema": "maf.test.v1",
                "payload": {"passed": True},
                "payload_sha256": "sha256:" + "0" * 64,
            },
            {
                "schema": "maf.test.v1",
                "payload": {"passed": True},
                "payload_sha256": canonical_sha256({"passed": True}),
                "extra": False,
            },
        ):
            with self.subTest(envelope=envelope):
                with self.assertRaises(CP7ArtifactValidationError):
                    parse_canonical_envelope_bytes(
                        canonical_json_bytes(envelope),
                        expected_schema="maf.test.v1",
                    )

    def test_restore_id_has_fixed_export_order_and_domain(self) -> None:
        kwargs = {
            "approval_request_id": "cp7a-" + "1" * 32,
            "release": "C_A",
            "phase": "authoritative_candidate",
            "commit": "2" * 40,
            "tree": "3" * 40,
            "daemon_reference": "example.invalid/dind@sha256:" + "4" * 64,
            "exports": (
                {"service": "backend", "sha256": "sha256:" + "5" * 64},
                {"service": "frontend", "sha256": "sha256:" + "6" * 64},
                {"service": "runtime-sidecar", "sha256": "sha256:" + "7" * 64},
            ),
        }

        restore_id = cp7_restore_id(**kwargs)

        self.assertEqual(
            restore_id,
            "cp7-restore-v1-e998e9acba0c23eddd734ad3a9b3718fcddfb4044e29252c7e20adfccff0e889",
        )
        self.assertEqual(restore_id, cp7_restore_id(**kwargs))
        reordered = dict(kwargs)
        reordered["exports"] = tuple(reversed(kwargs["exports"]))
        with self.assertRaises(CP7ArtifactValidationError):
            cp7_restore_id(**reordered)

    def test_runtime_authority_ids_follow_the_exact_contract(self) -> None:
        digest = "sha256:" + "a" * 64

        self.assertEqual(
            mcp_no_server_intent_id("task-1"),
            "mcp-no-server-intent:v1:task-1:initial",
        )
        self.assertEqual(
            mcp_no_server_intent_id("task-1", node_id="node-1"),
            "mcp-no-server-intent:v1:task-1:node-1",
        )
        self.assertEqual(
            mcp_dispatch_resume_outbox_id("intent-1"),
            "mcp-dispatch-resume:v1:intent-1",
        )
        self.assertEqual(
            mcp_terminal_candidate_id("call-1", digest),
            f"mcp-terminal-candidate:v1:call-1:{digest}",
        )
        self.assertEqual(
            mcp_terminal_receipt_id("call-1", digest),
            f"mcp-terminal-result:v1:call-1:{digest}",
        )
        self.assertEqual(
            mcp_terminal_projection_id("call-1"),
            "mcp-terminal-projection:v1:call-1",
        )


class CP7ImmutablePublicationTests(unittest.TestCase):
    def test_publish_is_no_clobber_and_secure_read_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "artifact.json"
            payload = canonical_json_bytes({"state": "pending"})

            published = publish_immutable(path, payload)

            self.assertEqual(published.content, payload)
            self.assertEqual(published.mode, 0o600)
            self.assertEqual(published.nlink, 1)
            self.assertEqual(secure_read(path).content, payload)
            with self.assertRaises(CP7ArtifactConflictError):
                publish_immutable(path, payload)

    def test_publish_or_compare_is_exactly_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "artifact.json"
            payload = b"same\n"

            first = publish_or_compare_immutable(path, payload)
            second = publish_or_compare_immutable(path, payload)

            self.assertTrue(first.created)
            self.assertFalse(second.created)
            with self.assertRaises(CP7ArtifactConflictError):
                publish_or_compare_immutable(path, b"different\n")

    def test_secure_envelope_read_binds_file_and_payload_digests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "artifact.json"
            payload = {"state": "pending_manual_approval"}
            publish_immutable(path, canonical_envelope_bytes("maf.test.v1", payload))

            result = secure_read_canonical_envelope(
                path,
                expected_schema="maf.test.v1",
            )

            self.assertEqual(result.payload, payload)
            self.assertEqual(result.payload_sha256, canonical_sha256(payload))
            self.assertEqual(result.artifact.file_sha256, canonical_sha256(result.envelope))

    def test_publication_fsyncs_file_and_directory_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "artifact.json"
            real_fsync = os.fsync
            calls: list[int] = []

            def recording_fsync(fd: int) -> None:
                calls.append(fd)
                real_fsync(fd)

            with patch("src.integrations.mcp.cp7_artifacts.os.fsync", recording_fsync):
                publish_immutable(path, b"durable\n")

            self.assertGreaterEqual(len(calls), 3)

    def test_concurrent_publication_has_one_winner_and_no_temp_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "artifact.json"

            def attempt(index: int) -> tuple[int, bool]:
                try:
                    publish_immutable(path, f"writer-{index}\n".encode())
                except CP7ArtifactConflictError:
                    return index, False
                return index, True

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(attempt, range(8)))

            winners = [index for index, won in results if won]
            self.assertEqual(len(winners), 1)
            self.assertEqual(secure_read(path).content, f"writer-{winners[0]}\n".encode())
            self.assertEqual([item.name for item in root.iterdir()], ["artifact.json"])

    def test_secure_read_rejects_symlink_hardlink_and_wrong_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "target"
            target.write_bytes(b"payload")
            target.chmod(0o600)
            symlink = root / "symlink"
            symlink.symlink_to(target)
            with self.assertRaises(CP7ArtifactValidationError):
                secure_read(symlink)

            hardlink = root / "hardlink"
            os.link(target, hardlink)
            with self.assertRaises(CP7ArtifactValidationError):
                secure_read(target)
            hardlink.unlink()

            target.chmod(0o644)
            with self.assertRaises(CP7ArtifactValidationError):
                secure_read(target)

    def test_secure_read_rejects_symlinked_parent_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            actual = root / "actual"
            actual.mkdir()
            artifact = actual / "artifact"
            artifact.write_bytes(b"payload")
            artifact.chmod(0o600)
            alias = root / "alias"
            alias.symlink_to(actual, target_is_directory=True)

            with self.assertRaises(CP7ArtifactValidationError):
                secure_read(alias / "artifact")

    def test_publish_and_secure_read_reject_mode_0755_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            insecure = root / "insecure"
            insecure.mkdir(mode=0o700)
            insecure.chmod(0o755)

            unpublished = insecure / "new-artifact"
            with self.assertRaises(CP7ArtifactValidationError):
                publish_immutable(unpublished, b"payload")
            self.assertFalse(unpublished.exists())
            self.assertEqual(tuple(insecure.iterdir()), ())

            existing = insecure / "existing-artifact"
            existing.write_bytes(b"payload")
            existing.chmod(0o600)
            with self.assertRaises(CP7ArtifactValidationError):
                secure_read(existing)


class CP7ClosedContractTests(unittest.TestCase):
    def test_route_reason_category_adds_only_no_server_value(self) -> None:
        self.assertEqual(
            MCPRouteReason.NO_USER_SCOPED_SERVER.value,
            "no_user_scoped_server",
        )
        with self.assertRaises(ValueError):
            MCPRouteReason("mcp_runtime_unavailable")
        self.assertEqual(
            MCPTerminalErrorCode.MCP_RUNTIME_UNAVAILABLE.value,
            "mcp_runtime_unavailable",
        )
        self.assertEqual(
            MCPUnavailableEventType.RUNTIME_UNAVAILABLE.value,
            "mcp.runtime_unavailable",
        )
        with self.assertRaises(ValueError):
            MCPTerminalErrorCode("no_user_scoped_server")
        with self.assertRaises(ValueError):
            MCPUnavailableEventType("mcp_runtime_unavailable")

    def test_core_rows_have_exact_design_fields(self) -> None:
        expected = {
            MCPNoServerIntent: (
                "intent_id",
                "owner_user_id",
                "task_id",
                "node_id",
                "trigger",
                "requested_server_id",
                "requested_server_config_version",
                "requested_server_security_version",
                "owner_server_set_fingerprint",
                "resume_envelope_json",
                "resume_envelope_sha256",
                "status",
                "revision",
                "evidence_sha256",
                "created_at",
                "updated_at",
                "terminal_at",
            ),
            UserMCPOwnerMutationGuard: (
                "owner_user_id",
                "revision",
                "server_set_fingerprint",
                "created_at",
                "updated_at",
            ),
            MCPDispatchResumeOutbox: (
                "outbox_id",
                "intent_id",
                "owner_user_id",
                "task_id",
                "node_id",
                "server_id",
                "resume_envelope_sha256",
                "payload_sha256",
                "status",
                "claim_owner",
                "claim_token",
                "lease_expires_at",
                "revision",
                "created_at",
                "updated_at",
                "completed_at",
                "result_receipt_id",
                "completion_mode",
            ),
            MCPValidatedTerminalResultCandidate: (
                "candidate_id",
                "owner_user_id",
                "conversation_id",
                "task_id",
                "node_id",
                "intent_id",
                "call_id",
                "server_id",
                "server_config_version",
                "server_security_version",
                "terminal_state",
                "result_payload_sha256",
                "safe_result_ref",
                "safe_result_ref_sha256",
                "safe_error_code",
                "sealed_at",
                "safe_result_content_sha256",
                "safe_result_size_bytes",
                "safe_result_store_kind",
            ),
            MCPTerminalResultReceipt: (
                "result_receipt_id",
                "candidate_id",
                "owner_user_id",
                "conversation_id",
                "task_id",
                "node_id",
                "intent_id",
                "call_id",
                "server_id",
                "server_config_version",
                "server_security_version",
                "terminal_state",
                "result_payload_sha256",
                "safe_result_ref",
                "safe_result_ref_sha256",
                "safe_error_code",
                "completion_mode",
                "committed_at",
                "safe_result_content_sha256",
                "safe_result_size_bytes",
                "safe_result_store_kind",
            ),
            MCPCP7SafetyLedgerRecord: (
                "record_id",
                "candidate_id",
                "epoch_id",
                "config_fingerprint",
                "record_kind",
                "red_line",
                "hook_id",
                "bucket_started_at",
                "bucket_ended_at",
                "reason_code",
                "value",
                "boundary_source_sha256",
                "payload_sha256",
                "recorded_at",
            ),
            MCPCP7ReadyEpochEvent: (
                "event_id",
                "candidate_id",
                "epoch_id",
                "predecessor_epoch_id",
                "event_kind",
                "container_id",
                "image_id",
                "config_fingerprint",
                "boundary_at",
                "audit_device",
                "audit_inode",
                "audit_offset",
                "ledger_record_count",
                "inflight_state_sha256",
                "payload_sha256",
            ),
            MCPCP7CandidateGuard: (
                "candidate_id",
                "invalid_latched",
                "first_invalid_record_id",
                "first_invalid_reason",
                "first_invalid_at",
                "created_at",
                "updated_at",
            ),
        }
        for model, names in expected.items():
            with self.subTest(model=model.__name__):
                self.assertEqual(tuple(item.name for item in fields(model)), names)

    def test_status_categories_are_separate_and_closed(self) -> None:
        self.assertEqual(
            MCPNoServerIntentTrigger.INITIAL_NO_PROFILE.value,
            "initial_no_profile",
        )
        self.assertEqual(MCPNoServerIntentStatus.UNKNOWN.value, "unknown")
        with self.assertRaises(ValueError):
            MCPNoServerIntentStatus("completed")

    def test_remaining_step_one_models_exist_as_frozen_slots(self) -> None:
        for model in (
            MCPDispatchResumeOutbox,
            MCPExecutionTerminalProjection,
            MCPCP7SafetyLedgerRecord,
            MCPCP7ReadyEpochEvent,
            MCPCP7CandidateGuard,
        ):
            self.assertTrue(model.__dataclass_params__.frozen)
            self.assertTrue(hasattr(model, "__slots__"))


if __name__ == "__main__":
    unittest.main()

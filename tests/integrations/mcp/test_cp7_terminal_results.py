from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.core.models import MCPValidatedTerminalResultCandidate, MCPTerminalState
from src.integrations.mcp.cp7_artifacts import (
    CP7ArtifactConflictError,
    canonical_envelope_bytes,
    canonical_sha256,
    mcp_terminal_candidate_id,
    publish_immutable,
)
from src.integrations.mcp.cp7_terminal_results import (
    CP7TerminalResultCorruptionError,
    CP7TerminalResultLimitError,
    TERMINAL_CANDIDATE_SCHEMA,
    compare_terminal_result_candidate,
    enumerate_unconsumed_terminal_result_candidates,
    seal_terminal_result_candidate,
    secure_read_terminal_result_candidate,
    terminal_call_index_path,
    terminal_candidate_path,
    terminal_task_index_path,
)


_RESULT_DIGEST = "sha256:" + "a" * 64
_SAFE_REF = "result-store://task-1/call-1"
_SEALED_AT = datetime(2026, 8, 13, 12, 34, 56, tzinfo=timezone.utc)


def _completed_candidate(
    *,
    call_id: str = "call-1",
    task_id: str = "task-1",
    result_payload_sha256: str = _RESULT_DIGEST,
) -> MCPValidatedTerminalResultCandidate:
    return MCPValidatedTerminalResultCandidate(
        candidate_id=mcp_terminal_candidate_id(call_id, result_payload_sha256),
        owner_user_id="owner-1",
        conversation_id="conversation-1",
        task_id=task_id,
        node_id="node-1",
        intent_id="intent-1",
        call_id=call_id,
        server_id="server-1",
        server_config_version=3,
        server_security_version=7,
        terminal_state=MCPTerminalState.COMPLETED,
        result_payload_sha256=result_payload_sha256,
        safe_result_ref=_SAFE_REF,
        safe_result_ref_sha256=canonical_sha256(_SAFE_REF),
        safe_error_code=None,
        sealed_at=_SEALED_AT,
    )


class CP7TerminalResultSealTests(unittest.TestCase):
    def test_candidate_and_indexes_have_exact_stable_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = _completed_candidate()

            outcome = seal_terminal_result_candidate(root, candidate)

            self.assertTrue(outcome.candidate_created)
            self.assertTrue(outcome.task_index_created)
            self.assertTrue(outcome.call_index_created)
            self.assertEqual(outcome.sealed.candidate, candidate)
            self.assertEqual(
                terminal_candidate_path(root, candidate.candidate_id).name,
                "candidate-b07718c1b3dc14c6e7fe64af7e66928b63a80c90f4e2d18765fb2d6fe3286dff.json",
            )
            self.assertEqual(
                terminal_call_index_path(root, candidate.call_id).name,
                "call-index-5d7963c4f471e142f5a72214a9666fb164718f9ba1066a7862ac1c5041887940.json",
            )
            self.assertEqual(
                outcome.sealed.candidate_payload_sha256,
                "sha256:c6c18bf2e7a76b20878535809ea140395c21c737e881e046897bd57284f78b1d",
            )
            raw = terminal_candidate_path(root, candidate.candidate_id).read_bytes()
            self.assertEqual(
                raw,
                canonical_envelope_bytes(
                    TERMINAL_CANDIDATE_SCHEMA,
                    {
                        "candidate_id": candidate.candidate_id,
                        "owner_user_id": "owner-1",
                        "conversation_id": "conversation-1",
                        "task_id": "task-1",
                        "node_id": "node-1",
                        "intent_id": "intent-1",
                        "call_id": "call-1",
                        "server_id": "server-1",
                        "server_config_version": 3,
                        "server_security_version": 7,
                        "terminal_state": "completed",
                        "result_payload_sha256": _RESULT_DIGEST,
                        "safe_result_ref": _SAFE_REF,
                        "safe_result_ref_sha256": canonical_sha256(_SAFE_REF),
                        "safe_error_code": None,
                        "sealed_at": "2026-08-13T12:34:56Z",
                    },
                ),
            )

    def test_response_loss_retry_is_exactly_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = _completed_candidate()

            first = seal_terminal_result_candidate(directory, candidate)
            retry = seal_terminal_result_candidate(directory, candidate)

            self.assertTrue(first.candidate_created)
            self.assertFalse(retry.candidate_created)
            self.assertFalse(retry.task_index_created)
            self.assertFalse(retry.call_index_created)
            self.assertEqual(retry.sealed, first.sealed)
            self.assertEqual(
                compare_terminal_result_candidate(directory, candidate), first.sealed
            )

    def test_seal_before_database_crash_is_enumerated_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = _completed_candidate()
            real_publish = __import__(
                "src.integrations.mcp.cp7_terminal_results",
                fromlist=["publish_or_compare_immutable"],
            ).publish_or_compare_immutable
            calls = 0

            def fail_after_candidate(*args: object, **kwargs: object) -> object:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated process loss before database commit")
                return real_publish(*args, **kwargs)

            with patch(
                "src.integrations.mcp.cp7_terminal_results.publish_or_compare_immutable",
                side_effect=fail_after_candidate,
            ):
                with self.assertRaises(RuntimeError):
                    seal_terminal_result_candidate(root, candidate)

            self.assertTrue(
                terminal_candidate_path(root, candidate.candidate_id).exists()
            )
            with self.assertRaises(CP7TerminalResultCorruptionError):
                enumerate_unconsumed_terminal_result_candidates(root)

            repaired = seal_terminal_result_candidate(root, candidate)
            self.assertFalse(repaired.candidate_created)
            self.assertEqual(
                enumerate_unconsumed_terminal_result_candidates(root),
                (repaired.sealed,),
            )

    def test_completed_and_error_nullable_contracts_are_closed(self) -> None:
        failed = replace(
            _completed_candidate(),
            terminal_state=MCPTerminalState.FAILED,
            safe_result_ref=None,
            safe_result_ref_sha256=None,
            safe_error_code="remote_tool_failed",
        )
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                seal_terminal_result_candidate(directory, failed).sealed.candidate,
                failed,
            )
        invalid = replace(
            _completed_candidate(), safe_result_ref_sha256="sha256:" + "0" * 64
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CP7TerminalResultCorruptionError):
                seal_terminal_result_candidate(directory, invalid)

    def test_same_candidate_id_with_binding_drift_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = _completed_candidate()
            seal_terminal_result_candidate(directory, original)

            with self.assertRaises(CP7ArtifactConflictError):
                seal_terminal_result_candidate(
                    directory,
                    replace(original, server_config_version=4),
                )


class CP7TerminalResultEnumerationTests(unittest.TestCase):
    def test_enumeration_is_deterministic_and_excludes_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            second = _completed_candidate(
                call_id="call-2",
                task_id="task-2",
                result_payload_sha256="sha256:" + "b" * 64,
            )
            first = _completed_candidate()
            seal_terminal_result_candidate(directory, second)
            seal_terminal_result_candidate(directory, first)

            all_candidates = enumerate_unconsumed_terminal_result_candidates(directory)
            remaining = enumerate_unconsumed_terminal_result_candidates(
                directory,
                consumed_candidate_ids={first.candidate_id},
            )

            self.assertEqual(
                tuple(item.candidate.candidate_id for item in all_candidates),
                tuple(
                    sorted((first.candidate_id, second.candidate_id), key=str.encode)
                ),
            )
            self.assertEqual(remaining[0].candidate, second)
            self.assertEqual(len(remaining), 1)

    def test_missing_or_corrupt_index_blocks_enumeration(self) -> None:
        for corrupt in (False, True):
            with (
                self.subTest(corrupt=corrupt),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                candidate = _completed_candidate()
                seal_terminal_result_candidate(root, candidate)
                index_path = terminal_call_index_path(root, candidate.call_id)
                if corrupt:
                    index_path.write_bytes(b"{}\n")
                    index_path.chmod(0o600)
                else:
                    index_path.unlink()

                with self.assertRaises(CP7TerminalResultCorruptionError):
                    enumerate_unconsumed_terminal_result_candidates(root)

    def test_multiple_candidates_for_one_call_block_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _completed_candidate()
            second = replace(
                first,
                result_payload_sha256="sha256:" + "c" * 64,
                candidate_id=mcp_terminal_candidate_id("call-1", "sha256:" + "c" * 64),
            )
            seal_terminal_result_candidate(root, first)
            with self.assertRaises(CP7ArtifactConflictError):
                seal_terminal_result_candidate(root, second)

            with self.assertRaises(CP7TerminalResultCorruptionError):
                enumerate_unconsumed_terminal_result_candidates(root)
            with self.assertRaises(CP7TerminalResultCorruptionError):
                secure_read_terminal_result_candidate(root, first.candidate_id)

    def test_index_fork_blocks_ready_and_old_receipts_do_not_expand_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = _completed_candidate()
            seal_terminal_result_candidate(root, candidate)
            existing_index = terminal_task_index_path(
                root, candidate.task_id, candidate.candidate_id
            )
            publish_immutable(
                root.resolve() / ("task-index-" + "f" * 64 + ".json"),
                existing_index.read_bytes(),
            )

            with self.assertRaises(CP7TerminalResultCorruptionError):
                enumerate_unconsumed_terminal_result_candidates(root)

        with tempfile.TemporaryDirectory() as directory:
            candidate = _completed_candidate()
            seal_terminal_result_candidate(directory, candidate)
            remaining = enumerate_unconsumed_terminal_result_candidates(
                directory,
                consumed_candidate_ids={
                    candidate.candidate_id,
                    "retained-receipt-only",
                },
            )
            self.assertEqual(remaining, ())

    def test_enumeration_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            seal_terminal_result_candidate(directory, _completed_candidate())
            with self.assertRaises(CP7TerminalResultLimitError):
                enumerate_unconsumed_terminal_result_candidates(
                    directory, maximum_entries=2
                )


class CP7TerminalResultPathSecurityTests(unittest.TestCase):
    def test_hostile_identifiers_cannot_control_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = _completed_candidate(
                call_id="../../call/雪",
                task_id="../task/雪",
            )

            sealed = seal_terminal_result_candidate(root, candidate).sealed

            self.assertEqual(sealed.candidate, candidate)
            self.assertEqual(len(tuple(root.iterdir())), 3)
            self.assertTrue(all(path.parent == root for path in root.iterdir()))
            self.assertEqual(
                secure_read_terminal_result_candidate(root, candidate.candidate_id),
                sealed,
            )

    def test_symlink_root_and_broad_root_permissions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            actual = parent / "actual"
            actual.mkdir(mode=0o700)
            alias = parent / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            with self.assertRaises(CP7TerminalResultCorruptionError):
                seal_terminal_result_candidate(alias, _completed_candidate())

            actual.chmod(0o755)
            with self.assertRaises(CP7TerminalResultCorruptionError):
                enumerate_unconsumed_terminal_result_candidates(actual)


if __name__ == "__main__":
    unittest.main()

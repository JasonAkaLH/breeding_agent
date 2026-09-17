from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.api.submission_admission import build_submission_admission_request
from src.core.enums import RoutingMode, TaskStatus
from src.core.models import Task
from src.integrations.mcp.result_parsing.json_values import canonical_json_bytes
from src.orchestration.models import UserMCPServerProfile
from src.storage.runtime_sidecar_facade import (
    validate_runtime_sidecar_submission_envelopes,
)


_REQUEST_KEYS = {
    "schema",
    "username",
    "conversation_id",
    "message_id",
    "role",
    "content",
    "routing_mode",
    "requested_capability_id",
    "model_edition",
    "model_options",
    "bundle_revisions",
    "execution_metadata",
    "upload_ids",
    "sheet_selections",
    "mcp_binding",
    "mcp_assignment",
    "pending_context",
}
_CONVERSATION_KEYS = {
    "schema",
    "conversation_id",
    "username",
    "status",
    "current_task_id",
    "created_at",
    "updated_at",
    "create_if_missing",
}
_MESSAGE_KEYS = {
    "schema",
    "message_id",
    "conversation_id",
    "role",
    "content",
    "task_id",
    "stream_status",
    "message_created_at",
    "message_type",
    "metadata",
    "updated_at",
}
_CONTINUATION_KEYS = {
    "schema",
    "request_fingerprint",
    "conversation_id",
    "message_id",
    "task_id",
    "owner_scope",
    "message_content_sha256",
    "routing_mode",
    "requested_capability_id",
    "model_options",
    "bundle_revisions",
    "execution_metadata",
    "upload_refs",
    "sheet_selections",
    "mcp_binding",
    "mcp_assignment",
    "available_mcp_servers",
    "pending_context",
    "initial_no_server_eligible",
    "skill_activation",
}


class SubmissionAdmissionRequestBuilderTests(unittest.TestCase):
    def test_exact_envelopes_and_collection_normalization(self) -> None:
        values = _builder_values()
        request = build_submission_admission_request(**values)
        conversation, message, continuation = _validated(request)

        self.assertEqual(set(conversation), _CONVERSATION_KEYS)
        self.assertEqual(set(message), _MESSAGE_KEYS)
        self.assertEqual(set(continuation), _CONTINUATION_KEYS)
        self.assertEqual(
            [ref["upload_id"] for ref in continuation["upload_refs"]],
            ["conversation-upload", "upload-a", "upload-b"],
        )
        self.assertEqual(
            continuation["sheet_selections"],
            {"conversation-upload": "Archive", "upload-a": "Sheet A"},
        )
        self.assertEqual(
            [server["server_id"] for server in continuation["available_mcp_servers"]],
            ["server-a", "server-b"],
        )
        self.assertEqual(
            continuation["pending_context"]["missing_requirements"],
            ["email", "name"],
        )

        canonical_request = _canonical_request(values)
        self.assertEqual(set(canonical_request), _REQUEST_KEYS)
        self.assertEqual(len(canonical_request), 17)
        self.assertEqual(canonical_request["upload_ids"], ["upload-a", "upload-b"])
        self.assertEqual(
            canonical_request["sheet_selections"],
            {"upload-a": "Sheet A", "upload-b": "Sheet B"},
        )
        self.assertEqual(
            request.request_fingerprint,
            hashlib.sha256(
                b"maf.submission.request.v1\0"
                + canonical_json_bytes(canonical_request)
            ).hexdigest(),
        )

    def test_nullable_fields_are_explicit(self) -> None:
        values = _builder_values(
            model_options={
                "model_edition": None,
                "reasoning_effort": "medium",
                "thinking_enabled": False,
            },
            bundle_revisions={
                "skill_bundle_revision": None,
                "mcp_bundle_revision": None,
            },
            explicit_upload_ids=(),
            request_sheet_selections={},
            upload_refs=(),
            mcp_binding=None,
            mcp_assignment=None,
            available_mcp_servers=(),
            pending_context=None,
        )
        request = build_submission_admission_request(**values)
        _, _, continuation = _validated(request)
        canonical_request = _canonical_request(values)

        self.assertIsNone(canonical_request["model_edition"])
        self.assertIsNone(canonical_request["requested_capability_id"])
        self.assertIsNone(canonical_request["mcp_binding"])
        self.assertIsNone(canonical_request["mcp_assignment"])
        self.assertIsNone(canonical_request["pending_context"])
        self.assertIsNone(continuation["requested_capability_id"])
        self.assertIsNone(continuation["mcp_binding"])
        self.assertIsNone(continuation["mcp_assignment"])
        self.assertIsNone(continuation["pending_context"])
        self.assertIsNone(continuation["skill_activation"])

    def test_projection_and_continuation_use_the_approved_digest_domains(self) -> None:
        request = build_submission_admission_request(**_builder_values())

        self.assertEqual(
            request.projection_sha256,
            hashlib.sha256(
                b"maf.submission.projection.v1\0"
                + request.conversation_projection
                + b"\0"
                + request.message_projection
            ).hexdigest(),
        )
        self.assertEqual(
            request.continuation_sha256,
            hashlib.sha256(
                b"maf.submission.continuation.v1\0" + request.continuation
            ).hexdigest(),
        )

    def test_existing_and_new_conversation_projection_are_explicit(self) -> None:
        new_request = build_submission_admission_request(
            **_builder_values(create_conversation_if_missing=True)
        )
        existing_request = build_submission_admission_request(
            **_builder_values(create_conversation_if_missing=False)
        )
        new_conversation, _, _ = _validated(new_request)
        existing_conversation, _, _ = _validated(existing_request)

        self.assertIs(new_conversation["create_if_missing"], True)
        self.assertIs(existing_conversation["create_if_missing"], False)
        self.assertEqual(new_request.request_fingerprint, existing_request.request_fingerprint)
        self.assertNotEqual(new_request.projection_sha256, existing_request.projection_sha256)

    def test_business_drift_changes_fingerprint_but_resolved_context_does_not(self) -> None:
        baseline_values = _builder_values()
        baseline = build_submission_admission_request(**baseline_values)
        drift_values = {
            "content": "changed content",
            "model_options": {
                **baseline_values["model_options"],
                "model_edition": "pro-v2",
            },
            "task": replace(
                baseline_values["task"],
                routing_mode=RoutingMode.FORCE_CAPABILITY,
                requested_capability_id="mcp.dispatch",
            ),
            "explicit_upload_ids": ("upload-c",),
            "request_sheet_selections": {"upload-a": "Different"},
            "mcp_binding": {
                **baseline_values["mcp_binding"],
                "server_id": "server-changed",
            },
            "mcp_assignment": {
                **baseline_values["mcp_assignment"],
                "route_reason_code": "changed",
            },
            "bundle_revisions": {
                **baseline_values["bundle_revisions"],
                "skill_bundle_revision": "skill-v2",
            },
            "pending_context": {
                **baseline_values["pending_context"],
                "assistant_message": "changed prompt",
            },
        }
        for field, changed in drift_values.items():
            with self.subTest(field=field):
                values = dict(baseline_values)
                values[field] = changed
                if field == "explicit_upload_ids":
                    values["request_sheet_selections"] = {}
                self.assertNotEqual(
                    build_submission_admission_request(**values).request_fingerprint,
                    baseline.request_fingerprint,
                )

        resolved_values = dict(baseline_values)
        resolved_values["upload_refs"] = baseline_values["upload_refs"] + (
            {
                "upload_id": "conversation-extra",
                "conversation_id": "conversation-1",
                "sha256": "d" * 64,
                "size_bytes": 4,
                "selected_sheet": None,
            },
        )
        resolved_values["available_mcp_servers"] = baseline_values[
            "available_mcp_servers"
        ] + (
            UserMCPServerProfile(
                "server-c", "Server C", "Archive", "streamable_http"
            ),
        )
        resolved = build_submission_admission_request(**resolved_values)
        self.assertEqual(resolved.request_fingerprint, baseline.request_fingerprint)
        self.assertNotEqual(resolved.continuation_sha256, baseline.continuation_sha256)


def _builder_values(**overrides: object) -> dict[str, object]:
    now = datetime(2026, 8, 27, 2, 3, 4, tzinfo=timezone.utc)
    values: dict[str, object] = {
        "username": "alice",
        "conversation_id": "conversation-1",
        "message_id": "message-1",
        "content": "hello",
        "task": Task(
            task_id="task-candidate",
            conversation_id="conversation-1",
            root_message_id="message-1",
            status=TaskStatus.ACCEPTED,
            routing_mode=RoutingMode.AUTO,
            requested_capability_id=None,
            created_at=now,
            updated_at=now,
        ),
        "conversation_created_at": now - timedelta(days=1),
        "conversation_updated_at": now,
        "create_conversation_if_missing": True,
        "message_created_at": now,
        "message_type": "chat",
        "message_metadata": {"safe": "metadata"},
        "model_options": {
            "model_edition": "pro",
            "reasoning_effort": "high",
            "thinking_enabled": True,
        },
        "bundle_revisions": {
            "skill_bundle_revision": "skill-v1",
            "mcp_bundle_revision": "mcp-v1",
        },
        "execution_metadata": _execution_metadata(),
        "explicit_upload_ids": ("upload-b", "upload-a", "upload-a"),
        "request_sheet_selections": {
            "upload-b": "Sheet B",
            "upload-a": "Sheet A",
        },
        "upload_refs": (
            {
                "upload_id": "upload-b",
                "conversation_id": "conversation-1",
                "sha256": "b" * 64,
                "size_bytes": 2,
                "selected_sheet": None,
            },
            {
                "upload_id": "conversation-upload",
                "conversation_id": "conversation-1",
                "sha256": "c" * 64,
                "size_bytes": 3,
                "selected_sheet": "Archive",
            },
            {
                "upload_id": "upload-a",
                "conversation_id": "conversation-1",
                "sha256": "a" * 64,
                "size_bytes": 1,
                "selected_sheet": "Sheet A",
            },
        ),
        "mcp_binding": {
            "server_id": "server-a",
            "server_config_version": 1,
            "server_security_version": 2,
            "display_name": "Server A",
            "command": "search",
            "binding_mode": "explicit",
        },
        "mcp_assignment": {
            "execution_mode": "user_scoped",
            "shadow_enabled": False,
            "rollout_config_version": "rollout-v1",
            "route_reason_code": "eligible",
            "rollout_mode": "enforce",
        },
        "available_mcp_servers": (
            UserMCPServerProfile(
                "server-b", "Server B", "Billing", "streamable_http"
            ),
            UserMCPServerProfile(
                "server-a", "Server A", "Search", "streamable_http"
            ),
        ),
        "pending_context": {
            "context_id": "pending-1",
            "capability_id": "skill.example",
            "original_user_message": "original",
            "assistant_message": "please provide details",
            "missing_requirements": ["name", "email", "name"],
        },
        "initial_no_server_eligible": False,
        "claim_owner": "api-worker",
        "claim_expires_at": now + timedelta(seconds=30),
    }
    values.update(overrides)
    return values


def _canonical_request(values: dict[str, object]) -> dict[str, object]:
    upload_ids = sorted(set(values["explicit_upload_ids"]))
    pending = dict(values["pending_context"]) if values["pending_context"] else None
    if pending is not None:
        pending["missing_requirements"] = sorted(set(pending["missing_requirements"]))
    model_options = dict(values["model_options"])
    task = values["task"]
    return {
        "schema": "maf.submission.request.v1",
        "username": values["username"],
        "conversation_id": values["conversation_id"],
        "message_id": values["message_id"],
        "role": "user",
        "content": values["content"],
        "routing_mode": str(task.routing_mode),
        "requested_capability_id": task.requested_capability_id,
        "model_edition": model_options["model_edition"],
        "model_options": model_options,
        "bundle_revisions": dict(values["bundle_revisions"]),
        "execution_metadata": dict(values["execution_metadata"]),
        "upload_ids": upload_ids,
        "sheet_selections": dict(values["request_sheet_selections"]),
        "mcp_binding": dict(values["mcp_binding"]) if values["mcp_binding"] else None,
        "mcp_assignment": (
            dict(values["mcp_assignment"]) if values["mcp_assignment"] else None
        ),
        "pending_context": pending,
    }


def _validated(request):
    return validate_runtime_sidecar_submission_envelopes(
        conversation_projection=request.conversation_projection,
        message_projection=request.message_projection,
        continuation=request.continuation,
        projection_sha256=request.projection_sha256,
        continuation_sha256=request.continuation_sha256,
        username=request.username,
        conversation_id=request.conversation_id,
        message_id=request.message_id,
        task_id=request.task.task_id,
        request_fingerprint=request.request_fingerprint,
        routing_mode=str(request.task.routing_mode),
        requested_capability_id=request.task.requested_capability_id,
    )


def _execution_metadata() -> dict[str, object]:
    return {
        "requested_capability_alias": None,
        "canonical_capability_id": None,
        "mcp_dispatch_server_id": None,
        "mcp_binding_mode": None,
        "mcp_command": None,
        "mcp_execution_mode": "user_scoped",
        "mcp_rollout_config_version": "rollout-v1",
        "mcp_route_reason_code": "eligible",
        "mcp_rollout_mode": "enforce",
        "defer_task_completed_until_pending_skill_context_processed": False,
        "forced_by_mcp_command": False,
        "mcp_shadow_enabled": False,
    }


if __name__ == "__main__":
    unittest.main()

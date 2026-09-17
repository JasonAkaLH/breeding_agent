from __future__ import annotations

import json
import socket
import ssl
import struct
from collections.abc import Mapping
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

from src.storage.rust_contract import load_runtime_sidecar_contract, resource_limit
from src.storage.runtime_sidecar_facade import (
    validate_runtime_sidecar_artifact_provenance,
    validate_runtime_sidecar_config_authority,
    validate_runtime_sidecar_endpoint,
    validate_runtime_sidecar_handshake,
    validate_runtime_sidecar_response,
    validate_runtime_sidecar_task_record,
)


_HTTP2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
_FRAME_DATA = 0x0
_FRAME_HEADERS = 0x1
_FRAME_RST_STREAM = 0x3
_FRAME_SETTINGS = 0x4
_FRAME_WINDOW_UPDATE = 0x8
_FRAME_GOAWAY = 0x7
_FLAG_END_STREAM = 0x1
_FLAG_END_HEADERS = 0x4
_FLAG_ACK = 0x1
_DEFAULT_HTTP2_MAX_FRAME_SIZE = 16_384
_DEFAULT_HTTP2_FLOW_WINDOW = 65_535
_SETTINGS_INITIAL_WINDOW_SIZE = 0x4
_SETTINGS_MAX_FRAME_SIZE = 0x5

_SUBMISSION_ADMISSION_DISPOSITIONS = {
    1: "created",
    2: "idempotent_replay",
    3: "conversation_busy",
    4: "message_id_conflict",
    5: "conversation_not_available",
}
_SUBMISSION_PROJECTION_STATES = {1: "pending", 2: "projected"}
_SUBMISSION_PREPARATION_STATES = {1: "pending", 2: "prepared"}
_SUBMISSION_HANDOFF_STATES = {1: "pending", 2: "handed_off"}
_MESSAGE_IDENTITY_KINDS = {
    1: "submission",
    2: "interrupt",
    3: "server_internal",
    4: "file_visible",
    5: "legacy_conflict_only",
}
_MESSAGE_IDENTITY_KIND_VALUES = {
    value: key for key, value in _MESSAGE_IDENTITY_KINDS.items()
}
_MESSAGE_IDENTITY_DISPOSITIONS = {
    1: "created",
    2: "exact_replay",
    3: "conflict",
    4: "conversation_not_available",
}
_CONVERSATION_ADMISSION_CLOSE_DISPOSITIONS = {
    1: "closed",
    2: "exact_replay",
    3: "conversation_not_available",
    4: "conflict",
}


class RuntimeSidecarGrpcClient:
    """Minimal dependency-free gRPC/h2c client for the Rust runtime sidecar.

    The project intentionally has no grpcio dependency yet. This client only
    implements the unary RuntimeSidecar subset needed by `maf-runtime-sidecar`
    for PRD 03 validation over loopback h2c, Unix sockets, or mTLS TCP; it is
    not a general gRPC stack.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        config_source: str = "environment_variable",
        allowed_hosts: tuple[str, ...] = (),
        mtls_enabled: bool = False,
        tls_ca_path: str | None = None,
        tls_cert_path: str | None = None,
        tls_key_path: str | None = None,
        tls_server_name: str | None = None,
        artifact_provenance: Mapping[str, Any] | None = None,
        allowed_artifact_checksums: tuple[str, ...] = (),
        allowed_cargo_lock_digests: tuple[str, ...] = (),
    ) -> None:
        if artifact_provenance is not None:
            validate_runtime_sidecar_artifact_provenance(
                artifact_provenance,
                allowed_checksums=set(allowed_artifact_checksums),
                allowed_cargo_lock_digests=set(allowed_cargo_lock_digests),
            )
        validated_endpoint = validate_runtime_sidecar_endpoint(
            endpoint,
            component="runtime_store",
            unavailable_error_code="runtime_store_unavailable",
            allowed_hosts=allowed_hosts,
        )
        parsed = urlparse(validated_endpoint)
        self._unix_socket_path: str | None = None
        self._tls_context: ssl.SSLContext | None = None
        self._tls_server_name: str | None = None
        self._scheme = "http"
        if parsed.scheme == "unix" and parsed.path:
            if not hasattr(socket, "AF_UNIX"):
                raise ValueError("runtime sidecar Unix socket endpoints are unavailable on this platform")
            self._host = "localhost"
            self._port = None
            self._authority = "localhost"
            self._unix_socket_path = parsed.path
            validate_runtime_sidecar_config_authority(
                "runtime_sidecar_endpoint",
                config_source,
                component="runtime_store",
                cross_host=False,
                mtls_enabled=mtls_enabled,
            )
            return
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
            raise ValueError(
                "runtime sidecar gRPC endpoint must be http://host:port, https://host:port, or unix:///path"
            )
        if parsed.scheme == "https":
            if not mtls_enabled:
                raise ValueError("runtime sidecar https endpoint requires mTLS to be enabled")
            if not tls_ca_path or not tls_cert_path or not tls_key_path:
                raise ValueError("runtime sidecar mTLS requires CA, client certificate, and client key paths")
        cross_host = _is_cross_host(parsed.hostname)
        if parsed.scheme == "http" and cross_host:
            raise ValueError("runtime sidecar cross-host endpoints must use https mTLS")
        validate_runtime_sidecar_config_authority(
            "runtime_sidecar_endpoint",
            config_source,
            component="runtime_store",
            cross_host=cross_host,
            mtls_enabled=mtls_enabled,
        )
        self._host = parsed.hostname
        self._port = parsed.port
        self._authority = f"{self._host}:{self._port}"
        self._scheme = parsed.scheme
        if parsed.scheme == "https":
            context = ssl.create_default_context(cafile=tls_ca_path)
            context.load_cert_chain(certfile=tls_cert_path, keyfile=tls_key_path)
            context.set_alpn_protocols(["h2"])
            self._tls_context = context
            self._tls_server_name = tls_server_name or self._host

    def version(self, *, timeout_seconds: float = 5) -> dict[str, Any]:
        payload = self._unary("Version", b"", timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        version = _decode_version_info(_first_message(fields, 1))
        validate_runtime_sidecar_handshake(version)
        return version

    def check_compatibility(self, *, timeout_seconds: float = 5) -> dict[str, Any]:
        contract = load_runtime_sidecar_contract()
        request = b"".join(
            [
                _field_string(1, "python-runtime-sidecar-client"),
                _field_string(2, str(contract["component"])),
                _field_string(3, str(contract["protocol_version"])),
                _field_string(4, str(contract["schema_hash"])),
                _field_string(5, str(contract["error_code_table_hash"])),
                b"".join(_field_string(6, str(feature)) for feature in contract["supported_features"]),
            ]
        )
        payload = self._unary("CheckCompatibility", request, timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        error = _optional_typed_error(fields, 4)
        if error is not None:
            _raise_typed_error(error)
        version = _decode_version_info(_first_message(fields, 2))
        response = {
            "compatible": _first_bool(fields, 1),
            "version": version,
            "missing_features": _all_strings(fields, 3),
            "error": None,
        }
        if not response["compatible"]:
            raise RuntimeError("runtime_store_protocol_incompatible: Rust runtime sidecar handshake is incompatible")
        validate_runtime_sidecar_handshake(version)
        return response

    def append_event(
        self,
        *,
        conversation_id: str,
        task_id: str,
        event_type: str,
        payload_json: bytes,
        idempotency_key: str,
        owner: str = "python-runtime",
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        return self._append_event_response(
            conversation_id=conversation_id,
            task_id=task_id,
            event_type=event_type,
            payload_json=payload_json,
            idempotency_key=idempotency_key,
            owner=owner,
            timeout_seconds=timeout_seconds,
        )["cursor"]

    def append_event_exact(
        self,
        *,
        conversation_id: str,
        task_id: str,
        event_type: str,
        payload_json: bytes,
        idempotency_key: str,
        owner: str = "python-runtime",
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        return self._append_event_response(
            conversation_id=conversation_id,
            task_id=task_id,
            event_type=event_type,
            payload_json=payload_json,
            idempotency_key=idempotency_key,
            owner=owner,
            timeout_seconds=timeout_seconds,
        )

    def _append_event_response(
        self,
        *,
        conversation_id: str,
        task_id: str,
        event_type: str,
        payload_json: bytes,
        idempotency_key: str,
        owner: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        idempotency = _field_string(1, idempotency_key) + _field_string(2, owner) + _field_varint(3, 0)
        request = b"".join(
            [
                _field_string(1, conversation_id),
                _field_string(2, task_id),
                _field_string(3, event_type),
                _field_bytes(4, payload_json),
                _field_bytes(5, idempotency),
            ]
        )
        payload = self._unary("AppendEvent", request, timeout_seconds=timeout_seconds)
        fields = _decode_closed_message(payload, 1, 2, 3)
        response = {
            "operation": "event_append",
            "cursor": _optional_event_cursor(fields, 1),
            "error": _optional_typed_error(fields, 2),
            "duplicate": _first_bool(fields, 3),
        }
        _consume_response("event_append", response)
        return response

    def submit_task(
        self,
        *,
        task_id: str,
        conversation_id: str,
        idempotency_key: str,
        task: Mapping[str, Any] | None = None,
        expected_from_status: str | None = None,
        owner: str = "python-runtime",
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        if task is not None:
            validate_runtime_sidecar_task_record(task)
            _ensure_task_identity(
                task,
                task_id=task_id,
                conversation_id=conversation_id,
                context="task_submit request",
            )
        idempotency = _field_string(1, idempotency_key) + _field_string(2, owner) + _field_varint(3, 0)
        request = _field_string(1, task_id) + _field_string(2, conversation_id) + _field_bytes(3, idempotency)
        if task is not None:
            request += _field_bytes(4, _task_record(task))
        if expected_from_status is not None:
            request += _field_string(5, expected_from_status)
        payload = self._unary("SubmitTask", request, timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        response = {
            "operation": "task_submit",
            "task_id": _first_string(fields, 1),
            "duplicate": _first_bool(fields, 2, default=False),
            "error": _optional_typed_error(fields, 3),
            "task": _optional_task_record(fields, 4),
        }
        _consume_response("task_submit", response)
        if task is not None and response["task"] is None:
            _raise_task_identity_invalid("task_submit response omitted the requested TaskRecord")
        if response["task"] is not None:
            _ensure_task_identity(
                response["task"],
                task_id=task_id,
                conversation_id=conversation_id,
                context="task_submit response",
            )
            if response["task_id"] != response["task"]["task_id"]:
                _raise_task_identity_invalid("task_submit response top-level task_id differs from TaskRecord")
        return response

    def get_task(self, *, task_id: str, timeout_seconds: float = 5) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        payload = self._unary("GetTask", _field_string(1, task_id), timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        response = {
            "operation": "task_get",
            "task": _optional_task_record(fields, 1),
            "found": _first_bool(fields, 2, default=False),
            "error": _optional_typed_error(fields, 3),
        }
        if response["error"] is not None:
            _raise_typed_error(response["error"])
        if response["found"] != (response["task"] is not None):
            raise RuntimeError("runtime_store_response_invalid: Rust runtime sidecar task_get response is inconsistent")
        if response["task"] is not None and response["task"]["task_id"] != task_id:
            _raise_task_identity_invalid("task_get response TaskRecord differs from requested task_id")
        return response

    def list_tasks_for_conversation(
        self,
        *,
        conversation_id: str,
        statuses: tuple[str, ...] = (),
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request = _field_string(1, conversation_id) + b"".join(
            _field_string(2, status) for status in statuses
        )
        payload = self._unary("ListTasksForConversation", request, timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        response = {
            "operation": "task_list_for_conversation",
            "tasks": [_decode_task_record(value) for value in fields.get(1, [])],
            "error": _optional_typed_error(fields, 2),
        }
        _consume_response("task_list_for_conversation", response)
        if any(task["conversation_id"] != conversation_id for task in response["tasks"]):
            _raise_task_identity_invalid(
                "task_list_for_conversation response contains a different conversation_id"
            )
        return response

    def get_active_task_for_conversation(
        self,
        *,
        conversation_id: str,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        payload = self._unary(
            "GetActiveTaskForConversation",
            _field_string(1, conversation_id),
            timeout_seconds=timeout_seconds,
        )
        fields = _decode_message(payload)
        response = {
            "operation": "task_get_active_for_conversation",
            "task": _optional_task_record(fields, 1),
            "found": _first_bool(fields, 2, default=False),
            "error": _optional_typed_error(fields, 3),
        }
        _consume_response("task_get_active_for_conversation", response)
        if response["task"] is not None and response["task"]["conversation_id"] != conversation_id:
            _raise_task_identity_invalid(
                "task_get_active_for_conversation response contains a different conversation_id"
            )
        return response

    def admit_submission(
        self,
        *,
        message_id: str,
        task_id: str,
        conversation_id: str,
        username: str,
        request_fingerprint: str,
        conversation_projection_json: bytes,
        message_projection_json: bytes,
        projection_sha256: str,
        continuation_json: bytes,
        continuation_sha256: str,
        message_created_at_ms: int,
        workflow_owner: str,
        now_ms: int,
        claim_ttl_ms: int,
        task: Mapping[str, Any],
        idempotency_key: str,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        validate_runtime_sidecar_task_record(task)
        request = b"".join(
            [
                _field_string(1, message_id),
                _field_string(2, task_id),
                _field_string(3, conversation_id),
                _field_string(4, username),
                _field_string(5, request_fingerprint),
                _field_bytes(6, conversation_projection_json),
                _field_bytes(7, message_projection_json),
                _field_string(8, projection_sha256),
                _field_bytes(9, continuation_json),
                _field_string(10, continuation_sha256),
                _field_varint(11, message_created_at_ms),
                _field_string(12, workflow_owner),
                _field_varint(13, now_ms),
                _field_varint(14, claim_ttl_ms),
                _field_bytes(15, _task_record(task)),
                _field_string(16, idempotency_key),
            ]
        )
        fields = _decode_closed_message(
            self._unary("AdmitSubmission", request, timeout_seconds=timeout_seconds),
            1,
            2,
            3,
            4,
        )
        response = {
            "operation": "submission_admit",
            "disposition": _enum_name(
                fields, 1, _SUBMISSION_ADMISSION_DISPOSITIONS
            ),
            "admission": _optional_submission_admission_record(fields, 2),
            "claim": _optional_submission_claim(fields, 3),
            "error": _optional_typed_error(fields, 4),
        }
        _consume_response("submission_admit", response)
        admission = response["admission"]
        if admission is not None and (
            admission["message_id"] != message_id
            or admission["conversation_id"] != conversation_id
            or admission["username"] != username
            or admission["request_fingerprint"] != request_fingerprint
            or admission["idempotency_key"] != idempotency_key
        ):
            _raise_task_identity_invalid(
                "submission_admit response identity differs from request"
            )
        if response["disposition"] == "created" and admission["task_id"] != task_id:
            _raise_task_identity_invalid(
                "created submission_admit response Task differs from request"
            )
        if response["claim"] is not None and response["claim"]["owner"] != workflow_owner:
            _raise_task_identity_invalid(
                "submission_admit response claim owner differs from request"
            )
        return response

    def claim_pending_submission(
        self,
        *,
        workflow_owner: str,
        now_ms: int,
        claim_ttl_ms: int,
        after_created_at_ms: int | None = None,
        after_message_id: str | None = None,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request = (
            _field_string(1, workflow_owner)
            + _field_varint(2, now_ms)
            + _field_varint(3, claim_ttl_ms)
        )
        if after_created_at_ms is not None:
            request += _field_varint(4, after_created_at_ms)
        if after_message_id is not None:
            request += _field_string(5, after_message_id)
        fields = _decode_closed_message(
            self._unary(
                "ClaimPendingSubmission", request, timeout_seconds=timeout_seconds
            ),
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
        )
        response = {
            "operation": "submission_pending_claim",
            "found": _first_bool(fields, 1),
            "admission": _optional_submission_admission_record(fields, 2),
            "claim": _optional_submission_claim(fields, 3),
            "authority_state": _first_string(fields, 4),
            "finalization_receipt_sha256": _optional_string(fields, 5),
            "error": _optional_typed_error(fields, 6),
            "pending_count": _first_int(fields, 7),
            "earliest_claim_expires_at_ms": _optional_int(fields, 8),
        }
        _consume_response("submission_pending_claim", response)
        earliest_claim_expires_at_ms = response["earliest_claim_expires_at_ms"]
        if (
            earliest_claim_expires_at_ms is not None
            and earliest_claim_expires_at_ms <= now_ms
        ):
            raise RuntimeError(
                "runtime_store_response_invalid: pending submission claim expiry is not in the future"
            )
        admission = response["admission"]
        if (
            admission is not None
            and after_created_at_ms is not None
            and after_message_id is not None
            and (admission["created_at_ms"], admission["message_id"])
            <= (after_created_at_ms, after_message_id)
        ):
            raise RuntimeError(
                "runtime_store_response_invalid: pending submission claim precedes its cursor"
            )
        if response["claim"] is not None and response["claim"]["owner"] != workflow_owner:
            _raise_task_identity_invalid(
                "submission_pending_claim response owner differs from request"
            )
        return response

    def renew_submission_claim(
        self,
        *,
        message_id: str,
        workflow_owner: str,
        claim_token: str,
        now_ms: int,
        claim_ttl_ms: int,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request = b"".join(
            [
                _field_string(1, message_id),
                _field_string(2, workflow_owner),
                _field_string(3, claim_token),
                _field_varint(4, now_ms),
                _field_varint(5, claim_ttl_ms),
            ]
        )
        fields = _decode_closed_message(
            self._unary(
                "RenewSubmissionClaim", request, timeout_seconds=timeout_seconds
            ),
            1,
            2,
        )
        response = {
            "operation": "submission_claim_renew",
            "claim": _optional_submission_claim(fields, 1),
            "error": _optional_typed_error(fields, 2),
        }
        _consume_response("submission_claim_renew", response)
        if response["claim"]["owner"] != workflow_owner:
            _raise_task_identity_invalid(
                "submission_claim_renew response owner differs from request"
            )
        return response

    def acknowledge_submission_projection(
        self,
        *,
        message_id: str,
        workflow_owner: str,
        claim_token: str,
        projection_sha256: str,
        now_ms: int,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        request = b"".join(
            [
                _field_string(1, message_id),
                _field_string(2, workflow_owner),
                _field_string(3, claim_token),
                _field_string(4, projection_sha256),
                _field_varint(5, 1),
                _field_varint(6, now_ms),
            ]
        )
        response = self._submission_admission_write(
            "AcknowledgeSubmissionProjection",
            "submission_projection_acknowledge",
            request,
            timeout_seconds=timeout_seconds,
        )
        admission = response["admission"]
        if (
            admission["message_id"] != message_id
            or admission["projection_sha256"] != projection_sha256
            or admission["projection_state"] != "projected"
        ):
            _raise_task_identity_invalid(
                "submission projection acknowledgement differs from request"
            )
        return response

    def prepare_submission_handoff(
        self,
        *,
        message_id: str,
        workflow_owner: str,
        claim_token: str,
        prepared_execution_json: bytes,
        prepared_execution_sha256: str,
        now_ms: int,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        request = b"".join(
            [
                _field_string(1, message_id),
                _field_string(2, workflow_owner),
                _field_string(3, claim_token),
                _field_bytes(4, prepared_execution_json),
                _field_string(5, prepared_execution_sha256),
                _field_varint(6, 1),
                _field_varint(7, now_ms),
            ]
        )
        response = self._submission_admission_write(
            "PrepareSubmissionHandoff",
            "submission_handoff_prepare",
            request,
            timeout_seconds=timeout_seconds,
        )
        admission = response["admission"]
        if (
            admission["message_id"] != message_id
            or admission["prepared_execution_sha256"]
            != prepared_execution_sha256
            or admission["prepared_execution_json"] != prepared_execution_json
            or admission["preparation_state"] != "prepared"
        ):
            _raise_task_identity_invalid(
                "submission preparation response differs from request"
            )
        return response

    def get_submission_preparation(
        self,
        *,
        username: str,
        conversation_id: str,
        task_id: str,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request = (
            _field_string(1, username)
            + _field_string(2, conversation_id)
            + _field_string(3, task_id)
        )
        fields = _decode_closed_message(
            self._unary(
                "GetSubmissionPreparation", request, timeout_seconds=timeout_seconds
            ),
            1,
            2,
            3,
        )
        response = {
            "operation": "submission_preparation_get",
            "found": _first_bool(fields, 1),
            "admission": _optional_submission_admission_record(fields, 2),
            "error": _optional_typed_error(fields, 3),
        }
        _consume_response("submission_preparation_get", response)
        admission = response["admission"]
        if admission is not None and (
            admission["username"] != username
            or admission["conversation_id"] != conversation_id
            or admission["task_id"] != task_id
            or admission["preparation_state"] != "prepared"
        ):
            _raise_task_identity_invalid(
                "submission preparation lookup differs from request"
            )
        return response

    def acknowledge_submission_handoff(
        self,
        *,
        message_id: str,
        workflow_owner: str,
        claim_token: str,
        prepared_execution_sha256: str,
        handoff_kind: str,
        handoff_identity: str,
        now_ms: int,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        request = b"".join(
            [
                _field_string(1, message_id),
                _field_string(2, workflow_owner),
                _field_string(3, claim_token),
                _field_string(4, prepared_execution_sha256),
                _field_string(5, handoff_kind),
                _field_string(6, handoff_identity),
                _field_varint(7, 1),
                _field_varint(8, now_ms),
            ]
        )
        response = self._submission_admission_write(
            "AcknowledgeSubmissionHandoff",
            "submission_handoff_acknowledge",
            request,
            timeout_seconds=timeout_seconds,
        )
        admission = response["admission"]
        if (
            admission["message_id"] != message_id
            or admission["prepared_execution_sha256"]
            != prepared_execution_sha256
            or admission["handoff_state"] != "handed_off"
            or admission["handoff_kind"] != handoff_kind
            or admission["handoff_identity"] != handoff_identity
        ):
            _raise_task_identity_invalid(
                "submission handoff acknowledgement differs from request"
            )
        return response

    def close_conversation_admission(
        self,
        *,
        username: str,
        conversation_id: str,
        operation_id: str,
        now_ms: int,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request = b"".join(
            [
                _field_string(1, username),
                _field_string(2, conversation_id),
                _field_string(3, operation_id),
                _field_varint(4, now_ms),
            ]
        )
        fields = _decode_closed_message(
            self._unary(
                "CloseConversationAdmission", request, timeout_seconds=timeout_seconds
            ),
            1,
            2,
            3,
        )
        response = {
            "operation": "conversation_admission_close",
            "disposition": _enum_name(
                fields, 1, _CONVERSATION_ADMISSION_CLOSE_DISPOSITIONS
            ),
            "revision": _first_int(fields, 2),
            "error": _optional_typed_error(fields, 3),
        }
        _consume_response("conversation_admission_close", response)
        return response

    def reserve_message_identity(
        self,
        *,
        identity: Mapping[str, Any],
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request_identity_payload = _message_identity_record(identity)
        requested = _decode_message_identity_record(request_identity_payload)
        fields = _decode_closed_message(
            self._unary(
                "ReserveMessageIdentity",
                _field_bytes(1, request_identity_payload),
                timeout_seconds=timeout_seconds,
            ),
            1,
            2,
            3,
        )
        response = {
            "operation": "message_identity_reserve",
            "disposition": _enum_name(
                fields, 1, _MESSAGE_IDENTITY_DISPOSITIONS
            ),
            "identity": _optional_message_identity_record(fields, 2),
            "error": _optional_typed_error(fields, 3),
        }
        _consume_response("message_identity_reserve", response)
        returned = response["identity"]
        disposition = response["disposition"]
        differs = disposition == "created" and returned != requested
        if disposition == "exact_replay" and returned is not None:
            replay_variable_fields = {"reserved_at_ms"}
            if requested["identity_kind"] == "interrupt":
                replay_variable_fields.add("message_created_at_ms")
            differs = any(
                returned[name] != value
                for name, value in requested.items()
                if name not in replay_variable_fields
            )
        if differs:
            _raise_task_identity_invalid(
                "message identity reservation response differs from request"
            )
        return response

    def _submission_admission_write(
        self,
        rpc_method: str,
        operation: str,
        request: bytes,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        fields = _decode_closed_message(
            self._unary(rpc_method, request, timeout_seconds=timeout_seconds),
            1,
            2,
            3,
        )
        response = {
            "operation": operation,
            "admission": _optional_submission_admission_record(fields, 1),
            "duplicate": _first_bool(fields, 2),
            "error": _optional_typed_error(fields, 3),
        }
        _consume_response(operation, response)
        return response

    def transition_node(
        self,
        *,
        task_id: str,
        node_id: str,
        to_status: str,
        expected_from_status: str,
        idempotency_key: str,
        node: Mapping[str, Any] | None = None,
        owner: str = "python-runtime",
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        idempotency = _field_string(1, idempotency_key) + _field_string(2, owner) + _field_varint(3, 0)
        request = (
            _field_string(1, task_id)
            + _field_string(2, node_id)
            + _field_string(3, expected_from_status)
            + _field_string(4, to_status)
            + _field_bytes(5, idempotency)
        )
        if node is not None:
            request += _field_bytes(6, _task_node_record(node))
        payload = self._unary("TransitionNode", request, timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        response = {
            "operation": "node_state_transition",
            "node_id": _first_string(fields, 1),
            "status": _first_string(fields, 2),
            "error": _optional_typed_error(fields, 3),
            "node": _optional_task_node_record(fields, 4),
        }
        _consume_response("node_state_transition", response)
        return response

    def commit_agent_state(
        self,
        *,
        operation: str,
        run: Mapping[str, Any],
        items: tuple[Mapping[str, Any], ...],
        expected_revision: int,
        expected_claim_token: str | None,
        idempotency_key: str,
        task_nodes: tuple[Mapping[str, Any], ...] = (),
        artifacts: tuple[Mapping[str, Any], ...] = (),
        final_projection_json: bytes | None = None,
        task: Mapping[str, Any] | None = None,
        owner: str = "python-runtime",
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request = (
            _field_string(1, operation)
            + _field_bytes(2, _agent_run_record(run))
            + b"".join(_field_bytes(3, _agent_item_record(item)) for item in items)
            + _field_varint(4, expected_revision)
        )
        if expected_claim_token is not None:
            request += _field_string(5, expected_claim_token)
        request += _field_bytes(6, _idempotency(idempotency_key, owner))
        request += b"".join(_field_bytes(7, _task_node_record(node)) for node in task_nodes)
        request += b"".join(
            _field_bytes(
                8,
                _artifact_record(
                    artifact_id=str(artifact["artifact_id"]),
                    task_id=str(artifact["task_id"]),
                    producer_node_id=str(artifact["producer_node_id"]),
                    artifact_type=str(artifact["artifact_type"]),
                    storage_ref=str(artifact["storage_ref"]),
                    summary=str(artifact.get("summary") or ""),
                    is_complete=bool(artifact["is_complete"]),
                    created_at=str(artifact["created_at"]),
                ),
            )
            for artifact in artifacts
        )
        if final_projection_json is not None:
            request += _field_bytes(9, final_projection_json)
        if task is not None:
            validate_runtime_sidecar_task_record(task)
            request += _field_bytes(10, _task_record(task))
        payload = self._unary("CommitAgentState", request, timeout_seconds=timeout_seconds)
        response = _agent_state_response("agent_state_commit", _decode_message(payload))
        _consume_response("agent_state_commit", response)
        if response["run"] is None or response["run"]["run_id"] != run["run_id"]:
            _raise_task_identity_invalid("Agent state response run identity differs from request")
        return response

    def get_agent_run(self, *, run_id: str, timeout_seconds: float = 5) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        payload = self._unary("GetAgentRun", _field_string(1, run_id), timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        run_payload = _first_message(fields, 1)
        response = {
            "operation": "agent_run_get",
            "run": _decode_agent_run_record(run_payload) if run_payload else None,
            "found": _first_bool(fields, 2, default=False),
            "error": _optional_typed_error(fields, 3),
        }
        _consume_response("agent_run_get", response)
        if response["run"] is not None and response["run"]["run_id"] != run_id:
            _raise_task_identity_invalid("AgentRun response identity differs from request")
        return response

    def get_agent_run_for_task(
        self, *, task_id: str, timeout_seconds: float = 5
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        payload = self._unary(
            "GetAgentRunForTask", _field_string(1, task_id), timeout_seconds=timeout_seconds
        )
        fields = _decode_message(payload)
        run_payload = _first_message(fields, 1)
        response = {
            "operation": "agent_run_get",
            "run": _decode_agent_run_record(run_payload) if run_payload else None,
            "found": _first_bool(fields, 2, default=False),
            "error": _optional_typed_error(fields, 3),
        }
        _consume_response("agent_run_get", response)
        if response["run"] is not None and response["run"]["task_id"] != task_id:
            _raise_task_identity_invalid("AgentRun response identity differs from requested Task")
        return response

    def list_agent_runs(
        self,
        *,
        statuses: tuple[str, ...] = (),
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request = b"".join(_field_string(1, status) for status in statuses)
        payload = self._unary(
            "ListAgentRuns",
            request,
            timeout_seconds=timeout_seconds,
        )
        fields = _decode_message(payload)
        runs = [_decode_agent_run_record(value) for value in fields.get(1, [])]
        response = {
            "operation": "agent_run_list",
            "runs": runs,
            "error": _optional_typed_error(fields, 2),
        }
        _consume_response("agent_run_list", response)
        if len({run["run_id"] for run in runs}) != len(runs):
            _raise_task_identity_invalid("AgentRun list contains duplicate run_id")
        return response

    def list_agent_items(self, *, run_id: str, timeout_seconds: float = 5) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        payload = self._unary("ListAgentItems", _field_string(1, run_id), timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        items = [_decode_agent_item_record(value) for value in fields.get(1, [])]
        response = {
            "operation": "agent_item_list",
            "items": items,
            "error": _optional_typed_error(fields, 2),
        }
        _consume_response("agent_item_list", response)
        if any(item["run_id"] != run_id for item in items):
            _raise_task_identity_invalid("AgentItem list contains a different run_id")
        return response

    def get_agent_final_projection(
        self, *, run_id: str, timeout_seconds: float = 5
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        payload = self._unary(
            "GetAgentFinalProjection", _field_string(1, run_id), timeout_seconds=timeout_seconds
        )
        fields = _decode_message(payload)
        projection = _first_message(fields, 1)
        response = {
            "operation": "agent_final_projection_get",
            "projection_json": projection or None,
            "found": _first_bool(fields, 2, default=False),
            "error": _optional_typed_error(fields, 3),
        }
        _consume_response("agent_final_projection_get", response)
        if response["found"] != (response["projection_json"] is not None):
            raise RuntimeError(
                "runtime_store_response_invalid: Agent final projection response is inconsistent"
            )
        return response

    def get_task_node(self, *, node_id: str, timeout_seconds: float = 5) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        payload = self._unary("GetTaskNode", _field_string(1, node_id), timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        response = {
            "operation": "task_node_get",
            "node": _optional_task_node_record(fields, 1),
            "found": _first_bool(fields, 2, default=False),
            "error": _optional_typed_error(fields, 3),
        }
        _consume_response("task_node_get", response)
        if response["node"] is not None and response["node"]["node_id"] != node_id:
            _raise_task_identity_invalid("task_node_get response differs from requested node_id")
        return response

    def list_task_nodes_for_task(self, *, task_id: str, timeout_seconds: float = 5) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        payload = self._unary(
            "ListTaskNodesForTask", _field_string(1, task_id), timeout_seconds=timeout_seconds
        )
        fields = _decode_message(payload)
        response = {
            "operation": "task_node_list",
            "nodes": [_decode_task_node_record(value) for value in fields.get(1, [])],
            "error": _optional_typed_error(fields, 2),
        }
        _consume_response("task_node_list", response)
        if any(node["task_id"] != task_id for node in response["nodes"]):
            _raise_task_identity_invalid("task_node_list response contains a different task_id")
        return response

    def save_artifact(
        self,
        *,
        artifact_id: str,
        task_id: str,
        producer_node_id: str,
        artifact_type: str,
        storage_ref: str,
        summary: str,
        is_complete: bool,
        created_at: str,
        idempotency_key: str,
        owner: str = "python-runtime",
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        artifact = _artifact_record(
            artifact_id=artifact_id,
            task_id=task_id,
            producer_node_id=producer_node_id,
            artifact_type=artifact_type,
            storage_ref=storage_ref,
            summary=summary,
            is_complete=is_complete,
            created_at=created_at,
        )
        request = _field_bytes(1, artifact) + _field_bytes(2, _idempotency(idempotency_key, owner))
        payload = self._unary("SaveArtifact", request, timeout_seconds=timeout_seconds)
        response = _artifact_response("artifact_save", _decode_message(payload))
        _consume_response("artifact_save", response)
        return response["artifact"]

    def get_artifact(
        self,
        *,
        artifact_id: str,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        payload = self._unary("GetArtifact", _field_string(1, artifact_id), timeout_seconds=timeout_seconds)
        response = _artifact_response("artifact_get", _decode_message(payload))
        _consume_response("artifact_get", response)
        return response

    def list_artifacts_for_task(
        self,
        *,
        task_id: str,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        payload = self._unary("ListArtifactsForTask", _field_string(1, task_id), timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        response = {
            "operation": "artifact_list",
            "artifacts": [_decode_artifact_record(value) for value in fields.get(1, [])],
            "error": _optional_typed_error(fields, 2),
        }
        _consume_response("artifact_list", response)
        return response

    def replay_events(
        self,
        *,
        conversation_id: str,
        task_id: str,
        after_sequence: int,
        page_limit: int,
        byte_limit: int,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request = b"".join(
            [
                _field_string(1, conversation_id),
                _field_string(2, task_id),
                _field_varint(3, after_sequence),
                _field_varint(4, page_limit),
                _field_varint(5, byte_limit),
            ]
        )
        payload = self._unary("ReplayEvents", request, timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        response = {
            "operation": "event_replay",
            "cursors": [_decode_event_cursor(value) for value in fields.get(1, [])],
            "truncated": _first_bool(fields, 2, default=False),
            "error": _optional_typed_error(fields, 3),
        }
        _consume_response("event_replay", response)
        return response

    def acquire_lease(
        self,
        *,
        task_id: str,
        owner_id: str,
        now_ms: int,
        ttl_ms: int,
        idempotency_key: str,
        owner: str = "python-runtime",
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request = b"".join(
            [
                _field_string(1, task_id),
                _field_string(2, owner_id),
                _field_varint(3, now_ms),
                _field_varint(4, ttl_ms),
                _field_bytes(5, _idempotency(idempotency_key, owner)),
            ]
        )
        payload = self._unary("AcquireLease", request, timeout_seconds=timeout_seconds)
        response = _lease_response("lease_acquire", _decode_message(payload))
        _consume_response("lease_acquire", response)
        return response

    def renew_lease(
        self,
        *,
        task_id: str,
        renew_token: str,
        now_ms: int,
        ttl_ms: int,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request = (
            _field_string(1, task_id)
            + _field_string(2, renew_token)
            + _field_varint(3, now_ms)
            + _field_varint(4, ttl_ms)
        )
        payload = self._unary("RenewLease", request, timeout_seconds=timeout_seconds)
        response = _lease_response("lease_renew", _decode_message(payload))
        _consume_response("lease_renew", response)
        return response

    def release_lease(
        self,
        *,
        task_id: str,
        renew_token: str,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request = _field_string(1, task_id) + _field_string(2, renew_token)
        payload = self._unary("ReleaseLease", request, timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        response = {
            "operation": "lease_release",
            "released": _first_bool(fields, 1, default=False),
            "error": _optional_typed_error(fields, 2),
        }
        _consume_response("lease_release", response)
        return response

    def write_cancellation_token(
        self,
        *,
        task_id: str,
        requested_at_ms: int,
        reason: str,
        terminal_policy: str,
        idempotency_key: str,
        owner: str = "python-runtime",
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request = b"".join(
            [
                _field_string(1, task_id),
                _field_varint(2, requested_at_ms),
                _field_string(3, reason),
                _field_string(4, terminal_policy),
                _field_bytes(5, _idempotency(idempotency_key, owner)),
            ]
        )
        payload = self._unary("WriteCancellationToken", request, timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        response = {
            "operation": "cancellation_token_write",
            "written": _first_bool(fields, 1, default=False),
            "error": _optional_typed_error(fields, 2),
        }
        _consume_response("cancellation_token_write", response)
        return response

    def pin_bundle_revision(
        self,
        *,
        task_id: str,
        bundle_kind: str,
        revision: str,
        idempotency_key: str,
        owner: str = "python-runtime",
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request = (
            _field_string(1, task_id)
            + _field_string(2, bundle_kind)
            + _field_string(3, revision)
            + _field_bytes(4, _idempotency(idempotency_key, owner))
        )
        payload = self._unary("PinBundleRevision", request, timeout_seconds=timeout_seconds)
        response = _bundle_revision_response("bundle_revision_pin", _decode_message(payload))
        _consume_response("bundle_revision_pin", response)
        return response

    def release_bundle_revision(
        self,
        *,
        task_id: str,
        bundle_kind: str,
        revision: str,
        released_at_ms: int,
        idempotency_key: str,
        owner: str = "python-runtime",
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        request = b"".join(
            [
                _field_string(1, task_id),
                _field_string(2, bundle_kind),
                _field_string(3, revision),
                _field_varint(4, released_at_ms),
                _field_bytes(5, _idempotency(idempotency_key, owner)),
            ]
        )
        payload = self._unary("ReleaseBundleRevision", request, timeout_seconds=timeout_seconds)
        response = _bundle_revision_response("bundle_revision_release", _decode_message(payload))
        _consume_response("bundle_revision_release", response)
        return response

    def _ensure_compatible(self, *, timeout_seconds: float) -> None:
        # This minimal h2c client opens a fresh TCP connection for each unary
        # call, so it replays the compatibility handshake before each runtime
        # operation instead of caching connection-scoped readiness in Python.
        self.check_compatibility(timeout_seconds=timeout_seconds)

    def _unary(self, method: str, protobuf_payload: bytes, *, timeout_seconds: float) -> bytes:
        path = f"/maf.runtime.v1.RuntimeSidecar/{method}"
        grpc_payload = b"\x00" + struct.pack(">I", len(protobuf_payload)) + protobuf_payload
        with self._connect(timeout_seconds=timeout_seconds) as sock:
            sock.settimeout(timeout_seconds)
            sock.sendall(_HTTP2_PREFACE)
            peer_max_frame_size, connection_window, stream_window = (
                _initialize_http2_connection(sock)
            )
            header_block = _encode_headers(
                [
                    (":method", "POST"),
                    (":scheme", self._scheme),
                    (":path", path),
                    (":authority", self._authority),
                    ("content-type", "application/grpc"),
                    ("te", "trailers"),
                    ("user-agent", "maf-runtime-sidecar-client/0.1"),
                ]
            )
            sock.sendall(_frame(_FRAME_HEADERS, _FLAG_END_HEADERS, 1, header_block))
            _send_grpc_payload(
                sock,
                grpc_payload,
                max_frame_size=peer_max_frame_size,
                connection_window=connection_window,
                stream_window=stream_window,
            )
            return _read_grpc_response(sock)

    def _connect(self, *, timeout_seconds: float) -> socket.socket:
        if self._unix_socket_path is not None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.settimeout(timeout_seconds)
                sock.connect(self._unix_socket_path)
            except BaseException:
                sock.close()
                raise
            return sock
        if self._port is None:
            raise RuntimeError("runtime sidecar TCP port is not configured")
        raw_sock = socket.create_connection((self._host, self._port), timeout=timeout_seconds)
        if self._tls_context is None:
            return raw_sock
        try:
            return self._tls_context.wrap_socket(raw_sock, server_hostname=self._tls_server_name)
        except BaseException:
            raw_sock.close()
            raise


def _consume_response(operation_name: str, response: Mapping[str, Any]) -> None:
    envelope = validate_runtime_sidecar_response(operation_name, response)
    error = envelope.get("error")
    if isinstance(error, Mapping):
        _raise_typed_error(error)


def _raise_typed_error(error: Mapping[str, Any]) -> None:
    raise RuntimeError(f"{error['code']}: {error['message']}")


def _read_grpc_response(sock: socket.socket) -> bytes:
    data = bytearray()
    max_message_bytes = resource_limit("grpc_max_message_bytes")
    while True:
        frame_type, flags, stream_id, payload = _read_frame(sock)
        if frame_type == _FRAME_SETTINGS and flags & _FLAG_ACK == 0:
            sock.sendall(_frame(_FRAME_SETTINGS, _FLAG_ACK, 0, b""))
            continue
        if frame_type == _FRAME_DATA and stream_id == 1:
            data.extend(payload)
            if len(data) > max_message_bytes + 5:
                raise RuntimeError(
                    "runtime_store_response_invalid: Rust runtime sidecar gRPC response exceeds the configured limit"
                )
            if flags & _FLAG_END_STREAM:
                break
            continue
        if frame_type == _FRAME_HEADERS and stream_id == 1:
            if flags & _FLAG_END_STREAM:
                break
            continue
        if frame_type == _FRAME_RST_STREAM:
            raise RuntimeError("runtime sidecar gRPC stream was reset")
        if frame_type == _FRAME_GOAWAY:
            raise RuntimeError("runtime sidecar gRPC connection received GOAWAY")
    if len(data) < 5:
        raise RuntimeError(
            "runtime_store_response_invalid: Rust runtime sidecar returned a truncated gRPC response"
        )
    compressed = data[0]
    if compressed:
        raise RuntimeError("runtime sidecar returned compressed gRPC payload")
    size = struct.unpack(">I", data[1:5])[0]
    if size > max_message_bytes or len(data) != size + 5:
        raise RuntimeError(
            "runtime_store_response_invalid: Rust runtime sidecar gRPC message length is inconsistent"
        )
    return bytes(data[5:])


def _grpc_data_frames(payload: bytes) -> tuple[bytes, ...]:
    chunks = tuple(
        payload[offset : offset + _DEFAULT_HTTP2_MAX_FRAME_SIZE]
        for offset in range(0, len(payload), _DEFAULT_HTTP2_MAX_FRAME_SIZE)
    ) or (b"",)
    return tuple(
        _frame(
            _FRAME_DATA,
            _FLAG_END_STREAM if index == len(chunks) - 1 else 0,
            1,
            chunk,
        )
        for index, chunk in enumerate(chunks)
    )


def _initialize_http2_connection(sock: socket.socket) -> tuple[int, int, int]:
    receive_window = resource_limit("grpc_max_message_bytes") + 5
    settings = (
        _SETTINGS_INITIAL_WINDOW_SIZE.to_bytes(2, "big")
        + receive_window.to_bytes(4, "big")
    )
    sock.sendall(_frame(_FRAME_SETTINGS, 0, 0, settings))
    sock.sendall(
        _frame(
            _FRAME_WINDOW_UPDATE,
            0,
            0,
            (receive_window - _DEFAULT_HTTP2_FLOW_WINDOW).to_bytes(4, "big"),
        )
    )
    max_frame_size = _DEFAULT_HTTP2_MAX_FRAME_SIZE
    stream_window = _DEFAULT_HTTP2_FLOW_WINDOW
    connection_window = _DEFAULT_HTTP2_FLOW_WINDOW
    while True:
        frame_type, flags, stream_id, payload = _read_frame(sock)
        if frame_type == _FRAME_SETTINGS:
            if flags & _FLAG_ACK:
                continue
            max_frame_size, stream_window = _apply_peer_settings(
                payload,
                max_frame_size=max_frame_size,
                stream_window=stream_window,
            )
            sock.sendall(_frame(_FRAME_SETTINGS, _FLAG_ACK, 0, b""))
            return max_frame_size, connection_window, stream_window
        if frame_type == _FRAME_WINDOW_UPDATE:
            increment = _window_update_increment(payload)
            if stream_id == 0:
                connection_window += increment
            elif stream_id == 1:
                stream_window += increment
            continue
        if frame_type in {_FRAME_GOAWAY, _FRAME_RST_STREAM}:
            raise RuntimeError(
                "runtime sidecar gRPC connection closed during HTTP/2 negotiation"
            )


def _send_grpc_payload(
    sock: socket.socket,
    payload: bytes,
    *,
    max_frame_size: int,
    connection_window: int,
    stream_window: int,
) -> None:
    offset = 0
    while offset < len(payload):
        while connection_window <= 0 or stream_window <= 0:
            frame_type, flags, stream_id, frame_payload = _read_frame(sock)
            if frame_type == _FRAME_WINDOW_UPDATE:
                increment = _window_update_increment(frame_payload)
                if stream_id == 0:
                    connection_window += increment
                elif stream_id == 1:
                    stream_window += increment
                continue
            if frame_type == _FRAME_SETTINGS:
                if flags & _FLAG_ACK == 0:
                    max_frame_size, stream_window = _apply_peer_settings(
                        frame_payload,
                        max_frame_size=max_frame_size,
                        stream_window=stream_window,
                    )
                    sock.sendall(_frame(_FRAME_SETTINGS, _FLAG_ACK, 0, b""))
                continue
            if frame_type in {_FRAME_GOAWAY, _FRAME_RST_STREAM}:
                raise RuntimeError(
                    "runtime sidecar gRPC connection closed while sending request"
                )
            raise RuntimeError(
                "runtime_store_response_invalid: Rust runtime sidecar responded before the unary request completed"
            )
        size = min(
            max_frame_size,
            connection_window,
            stream_window,
            len(payload) - offset,
        )
        end = offset + size
        sock.sendall(
            _frame(
                _FRAME_DATA,
                _FLAG_END_STREAM if end == len(payload) else 0,
                1,
                payload[offset:end],
            )
        )
        offset = end
        connection_window -= size
        stream_window -= size


def _apply_peer_settings(
    payload: bytes,
    *,
    max_frame_size: int,
    stream_window: int,
) -> tuple[int, int]:
    if len(payload) % 6:
        raise RuntimeError(
            "runtime_store_response_invalid: Rust runtime sidecar returned malformed HTTP/2 settings"
        )
    for offset in range(0, len(payload), 6):
        setting = int.from_bytes(payload[offset : offset + 2], "big")
        value = int.from_bytes(payload[offset + 2 : offset + 6], "big")
        if setting == _SETTINGS_INITIAL_WINDOW_SIZE:
            if value > 0x7FFF_FFFF:
                raise RuntimeError(
                    "runtime_store_response_invalid: Rust runtime sidecar flow-control window is invalid"
                )
            stream_window += value - _DEFAULT_HTTP2_FLOW_WINDOW
        elif setting == _SETTINGS_MAX_FRAME_SIZE:
            if not 16_384 <= value <= 16_777_215:
                raise RuntimeError(
                    "runtime_store_response_invalid: Rust runtime sidecar frame size is invalid"
                )
            max_frame_size = value
    return max_frame_size, stream_window


def _window_update_increment(payload: bytes) -> int:
    if len(payload) != 4:
        raise RuntimeError(
            "runtime_store_response_invalid: Rust runtime sidecar WINDOW_UPDATE is malformed"
        )
    increment = int.from_bytes(payload, "big") & 0x7FFF_FFFF
    if increment == 0:
        raise RuntimeError(
            "runtime_store_response_invalid: Rust runtime sidecar WINDOW_UPDATE is invalid"
        )
    return increment


def _frame(frame_type: int, flags: int, stream_id: int, payload: bytes) -> bytes:
    return len(payload).to_bytes(3, "big") + bytes([frame_type, flags]) + (stream_id & 0x7FFF_FFFF).to_bytes(4, "big") + payload


def _read_frame(sock: socket.socket) -> tuple[int, int, int, bytes]:
    header = _recv_exact(sock, 9)
    length = int.from_bytes(header[:3], "big")
    frame_type = header[3]
    flags = header[4]
    stream_id = int.from_bytes(header[5:9], "big") & 0x7FFF_FFFF
    return frame_type, flags, stream_id, _recv_exact(sock, length)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise RuntimeError("runtime sidecar gRPC connection closed unexpectedly")
        chunks.extend(chunk)
    return bytes(chunks)


def _encode_headers(headers: list[tuple[str, str]]) -> bytes:
    encoded = bytearray()
    for name, value in headers:
        name_bytes = name.encode("ascii")
        value_bytes = value.encode("ascii")
        encoded.append(0x00)  # Literal Header Field without Indexing, new name.
        encoded.extend(_hpack_length(name_bytes))
        encoded.extend(_hpack_length(value_bytes))
    return bytes(encoded)


def _hpack_length(value: bytes) -> bytes:
    return _hpack_integer(len(value), 7) + value


def _hpack_integer(value: int, prefix_bits: int) -> bytes:
    max_prefix = (1 << prefix_bits) - 1
    if value < max_prefix:
        return bytes([value])
    encoded = bytearray([max_prefix])
    value -= max_prefix
    while value >= 128:
        encoded.append((value % 128) + 128)
        value //= 128
    encoded.append(value)
    return bytes(encoded)


def _field_varint(field_number: int, value: int) -> bytes:
    return _varint((field_number << 3) | 0) + _varint(value)


def _field_string(field_number: int, value: str) -> bytes:
    return _field_bytes(field_number, value.encode("utf-8"))


def _field_bytes(field_number: int, value: bytes) -> bytes:
    return _varint((field_number << 3) | 2) + _varint(len(value)) + value


def _idempotency(key: str, owner: str) -> bytes:
    return _field_string(1, key) + _field_string(2, owner) + _field_varint(3, 0)


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _decode_message(
    payload: bytes,
    *,
    allowed_fields: frozenset[int] | None = None,
    singular_fields: frozenset[int] = frozenset(),
) -> dict[int, list[Any]]:
    fields: dict[int, list[Any]] = {}
    offset = 0
    try:
        while offset < len(payload):
            key, offset = _read_varint(payload, offset)
            field_number = key >> 3
            wire_type = key & 0x07
            if field_number == 0 or (
                allowed_fields is not None and field_number not in allowed_fields
            ):
                raise ValueError
            if field_number in singular_fields and field_number in fields:
                raise ValueError
            if wire_type == 0:
                value, offset = _read_varint(payload, offset)
            elif wire_type == 2:
                length, offset = _read_varint(payload, offset)
                end = offset + length
                if end > len(payload):
                    raise ValueError
                value = payload[offset:end]
                offset = end
            else:
                raise ValueError
            fields.setdefault(field_number, []).append(value)
    except (IndexError, ValueError) as exc:
        raise RuntimeError(
            "runtime_store_response_invalid: Rust runtime sidecar returned malformed protobuf"
        ) from exc
    return fields


def _read_varint(payload: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while True:
        if offset >= len(payload) or shift >= 70:
            raise ValueError("truncated or oversized protobuf varint")
        byte = payload[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, offset
        shift += 7


def _first_message(fields: dict[int, list[Any]], field_number: int) -> bytes:
    values = fields.get(field_number, [])
    return values[0] if values else b""


def _decode_closed_message(payload: bytes, *field_numbers: int) -> dict[int, list[Any]]:
    closed = frozenset(field_numbers)
    return _decode_message(
        payload,
        allowed_fields=closed,
        singular_fields=closed,
    )


def _all_strings(fields: dict[int, list[Any]], field_number: int) -> list[str]:
    return [bytes(value).decode("utf-8") for value in fields.get(field_number, [])]


def _first_bool(fields: dict[int, list[Any]], field_number: int, *, default: bool = False) -> bool:
    values = fields.get(field_number, [])
    return bool(values[0]) if values else default


def _decode_version_info(payload: bytes) -> dict[str, Any]:
    fields = _decode_message(payload)
    return {
        "component": _first_string(fields, 1),
        "build_version": _first_string(fields, 2),
        "protocol_version": _first_string(fields, 3),
        "schema_hash": _first_string(fields, 4),
        "error_code_table_hash": _first_string(fields, 5),
        "supported_features": _all_strings(fields, 6),
        "min_client_version": _first_string(fields, 7),
        "max_client_version": _first_string(fields, 8),
    }


def _decode_event_cursor(payload: bytes) -> dict[str, Any]:
    fields = _decode_message(payload)
    return {
        "conversation_id": _first_string(fields, 1),
        "task_id": _first_string(fields, 2),
        "sequence": _first_int(fields, 3),
        "created_at_ms": _first_int(fields, 4),
    }


def _optional_event_cursor(fields: dict[int, list[Any]], field_number: int) -> dict[str, Any] | None:
    values = fields.get(field_number, [])
    return _decode_event_cursor(values[0]) if values else None


def _enum_name(
    fields: dict[int, list[Any]],
    field_number: int,
    values: Mapping[int, str],
) -> str:
    encoded = _first_int(fields, field_number)
    try:
        return values[encoded]
    except KeyError as exc:
        raise RuntimeError(
            "runtime_store_response_invalid: Rust runtime sidecar returned an unknown enum value"
        ) from exc


def _decode_submission_claim(payload: bytes) -> dict[str, Any]:
    fields = _decode_closed_message(payload, 1, 2, 3)
    return {
        "owner": _first_string(fields, 1),
        "token": _first_string(fields, 2),
        "expires_at_ms": _first_int(fields, 3),
    }


def _optional_submission_claim(
    fields: dict[int, list[Any]], field_number: int
) -> dict[str, Any] | None:
    payload = _first_message(fields, field_number)
    return _decode_submission_claim(payload) if payload else None


def _message_identity_record(identity: Mapping[str, Any]) -> bytes:
    try:
        kind = _MESSAGE_IDENTITY_KIND_VALUES[str(identity["identity_kind"])]
    except (KeyError, TypeError) as exc:
        raise ValueError("unsupported runtime sidecar Message identity kind") from exc
    payload = b"".join(
        [
            _field_string(1, str(identity["message_id"])),
            _field_string(2, str(identity["conversation_id"])),
            _field_string(3, str(identity["username"])),
            _field_varint(4, kind),
            _field_varint(10, int(identity["reserved_at_ms"])),
        ]
    )
    for number, name in (
        (5, "role"),
        (6, "message_type"),
        (8, "task_id"),
        (9, "request_fingerprint"),
    ):
        if identity.get(name) is not None:
            payload += _field_string(number, str(identity[name]))
    if identity.get("message_created_at_ms") is not None:
        payload += _field_varint(7, int(identity["message_created_at_ms"]))
    return payload


def _decode_message_identity_record(payload: bytes) -> dict[str, Any]:
    fields = _decode_closed_message(payload, *range(1, 11))
    return {
        "message_id": _first_string(fields, 1),
        "conversation_id": _first_string(fields, 2),
        "username": _first_string(fields, 3),
        "identity_kind": _enum_name(fields, 4, _MESSAGE_IDENTITY_KINDS),
        "role": _optional_string(fields, 5),
        "message_type": _optional_string(fields, 6),
        "message_created_at_ms": _optional_int(fields, 7),
        "task_id": _optional_string(fields, 8),
        "request_fingerprint": _optional_string(fields, 9),
        "reserved_at_ms": _first_int(fields, 10),
    }


def _optional_message_identity_record(
    fields: dict[int, list[Any]], field_number: int
) -> dict[str, Any] | None:
    payload = _first_message(fields, field_number)
    return _decode_message_identity_record(payload) if payload else None


def _decode_submission_admission_record(payload: bytes) -> dict[str, Any]:
    fields = _decode_closed_message(payload, *range(1, 23))
    prepared = _first_message(fields, 13)
    task = _first_message(fields, 21)
    return {
        "message_id": _first_string(fields, 1),
        "task_id": _first_string(fields, 2),
        "conversation_id": _first_string(fields, 3),
        "username": _first_string(fields, 4),
        "request_fingerprint": _first_string(fields, 5),
        "conversation_projection_json": _first_message(fields, 6),
        "message_projection_json": _first_message(fields, 7),
        "projection_sha256": _first_string(fields, 8),
        "continuation_json": _first_message(fields, 9),
        "continuation_sha256": _first_string(fields, 10),
        "projection_state": _enum_name(fields, 11, _SUBMISSION_PROJECTION_STATES),
        "preparation_state": _enum_name(fields, 12, _SUBMISSION_PREPARATION_STATES),
        "prepared_execution_json": prepared or None,
        "prepared_execution_sha256": _optional_string(fields, 14),
        "handoff_state": _enum_name(fields, 15, _SUBMISSION_HANDOFF_STATES),
        "handoff_kind": _optional_string(fields, 16),
        "handoff_identity": _optional_string(fields, 17),
        "created_at_ms": _first_int(fields, 18),
        "updated_at_ms": _first_int(fields, 19),
        "closed": _first_bool(fields, 20),
        "task": _decode_task_record(task) if task else None,
        "idempotency_key": _first_string(fields, 22),
    }


def _optional_submission_admission_record(
    fields: dict[int, list[Any]], field_number: int
) -> dict[str, Any] | None:
    payload = _first_message(fields, field_number)
    return _decode_submission_admission_record(payload) if payload else None


def _task_route_assignment(assignment: Mapping[str, Any]) -> bytes:
    payload = b"".join(
        _field_string(number, str(assignment[name]))
        for number, name in enumerate(
            ("route_mode", "real_path", "shadow_path", "config_version", "reason_code"), start=1
        )
    )
    for number, name in ((6, "cohort_id"), (7, "assignment_key_hash"), (8, "assigned_at")):
        if assignment.get(name) is not None:
            payload += _field_string(number, str(assignment[name]))
    return payload


def _task_record(task: Mapping[str, Any]) -> bytes:
    payload = b"".join(
        [
            _field_string(1, str(task["task_id"])),
            _field_string(2, str(task["conversation_id"])),
            _field_string(3, str(task["root_message_id"])),
            _field_string(4, str(task["status"])),
            _field_string(5, str(task["routing_mode"])),
        ]
    )
    for number, name in (
        (6, "requested_capability_id"),
        (8, "summary"),
        (9, "cancel_requested_at"),
        (10, "created_at"),
        (11, "updated_at"),
    ):
        if task.get(name) is not None:
            payload += _field_string(number, str(task[name]))
    if task.get("assignment") is not None:
        payload += _field_bytes(12, _task_route_assignment(task["assignment"]))
    return payload


def _decode_task_route_assignment(payload: bytes) -> dict[str, Any]:
    fields = _decode_closed_message(payload, *range(1, 9))
    return {
        "route_mode": _first_string(fields, 1),
        "real_path": _first_string(fields, 2),
        "shadow_path": _first_string(fields, 3),
        "config_version": _first_string(fields, 4),
        "reason_code": _first_string(fields, 5),
        "cohort_id": _optional_string(fields, 6),
        "assignment_key_hash": _optional_string(fields, 7),
        "assigned_at": _optional_string(fields, 8),
    }


def _decode_task_record(payload: bytes) -> dict[str, Any]:
    fields = _decode_closed_message(payload, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12)
    assignment = _first_message(fields, 12)
    return {
        "task_id": _first_string(fields, 1),
        "conversation_id": _first_string(fields, 2),
        "root_message_id": _first_string(fields, 3),
        "status": _first_string(fields, 4),
        "routing_mode": _first_string(fields, 5),
        "requested_capability_id": _optional_string(fields, 6),
        "summary": _optional_string(fields, 8),
        "cancel_requested_at": _optional_string(fields, 9),
        "created_at": _optional_string(fields, 10),
        "updated_at": _optional_string(fields, 11),
        "assignment": _decode_task_route_assignment(assignment) if assignment else None,
    }


def _optional_task_record(fields: dict[int, list[Any]], field_number: int) -> dict[str, Any] | None:
    payload = _first_message(fields, field_number)
    return _decode_task_record(payload) if payload else None


def _task_node_record(node: Mapping[str, Any]) -> bytes:
    payload = b"".join(
        [
            _field_string(1, str(node["node_id"])),
            _field_string(2, str(node["task_id"])),
            _field_string(3, str(node["capability_id"])),
            _field_string(5, str(node["status"])),
        ]
    )
    for number, name in ((4, "assigned_instance_id"), (13, "started_at"), (14, "finished_at")):
        if node.get(name) is not None:
            payload += _field_string(number, str(node[name]))
    payload += b"".join(_field_string(11, str(value)) for value in node["input_refs"])
    payload += b"".join(_field_string(12, str(value)) for value in node["output_refs"])
    return payload


def _agent_run_record(run: Mapping[str, Any]) -> bytes:
    digests = run.get("binding_option_digests_json", b"{}")
    if isinstance(digests, Mapping):
        digests = json.dumps(digests, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload = b"".join(
        [
            _field_string(1, str(run["run_id"])),
            _field_string(2, str(run["task_id"])),
            _field_string(3, str(run["conversation_id"])),
            _field_string(4, str(run["status"])),
            _field_string(5, str(run["model_edition"])),
            _field_string(6, str(run["reasoning_effort"])),
            _field_varint(7, 1 if run.get("thinking_enabled") else 0),
            _field_bytes(8, bytes(digests)),
            _field_varint(9, int(run["next_item_sequence"])),
            _field_varint(10, int(run["compacted_through_sequence"])),
            b"".join(_field_string(12, str(value)) for value in run.get("waiting_call_item_ids", ())),
            _field_varint(13, int(run.get("next_batch_call_ordinal", 0))),
            _field_varint(17, int(run["revision"])),
            _field_varint(19, int(run["created_at_ms"])),
            _field_varint(20, int(run["updated_at_ms"])),
        ]
    )
    for number, name in ((11, "active_sample_item_id"), (14, "claim_owner"), (15, "claim_token"), (18, "terminal_reason_code")):
        if run.get(name) is not None:
            payload += _field_string(number, str(run[name]))
    for number, name in ((16, "lease_expires_at_ms"), (21, "terminal_at_ms")):
        if run.get(name) is not None:
            payload += _field_varint(number, int(run[name]))
    return payload


def _decode_agent_run_record(payload: bytes) -> dict[str, Any]:
    fields = _decode_message(payload)
    return {
        "run_id": _first_string(fields, 1),
        "task_id": _first_string(fields, 2),
        "conversation_id": _first_string(fields, 3),
        "status": _first_string(fields, 4),
        "model_edition": _first_string(fields, 5),
        "reasoning_effort": _first_string(fields, 6),
        "thinking_enabled": _first_bool(fields, 7, default=False),
        "binding_option_digests_json": _first_message(fields, 8),
        "next_item_sequence": _first_int(fields, 9),
        "compacted_through_sequence": _first_int(fields, 10),
        "active_sample_item_id": _optional_string(fields, 11),
        "waiting_call_item_ids": _all_strings(fields, 12),
        "next_batch_call_ordinal": _first_int(fields, 13),
        "claim_owner": _optional_string(fields, 14),
        "claim_token": _optional_string(fields, 15),
        "lease_expires_at_ms": _optional_int(fields, 16),
        "revision": _first_int(fields, 17),
        "terminal_reason_code": _optional_string(fields, 18),
        "created_at_ms": _first_int(fields, 19),
        "updated_at_ms": _first_int(fields, 20),
        "terminal_at_ms": _optional_int(fields, 21),
    }


def _agent_item_record(item: Mapping[str, Any]) -> bytes:
    payload = b"".join(
        [
            _field_string(1, str(item["item_id"])),
            _field_string(2, str(item["run_id"])),
            _field_string(3, str(item["task_id"])),
            _field_varint(4, int(item["sequence"])),
            _field_string(5, str(item["kind"])),
            _field_string(6, str(item["state"])),
            _field_bytes(7, bytes(item["payload_json"])),
            _field_varint(8, int(item["payload_size_bytes"])),
            _field_string(9, str(item["payload_sha256"])),
            _field_varint(14, int(item["created_at_ms"])),
        ]
    )
    for number, name in ((10, "parent_item_id"), (11, "source_call_item_id"), (12, "provider_sample_id")):
        if item.get(name) is not None:
            payload += _field_string(number, str(item[name]))
    for number, name in ((13, "call_ordinal"), (15, "committed_at_ms")):
        if item.get(name) is not None:
            payload += _field_varint(number, int(item[name]))
    return payload


def _decode_agent_item_record(payload: bytes) -> dict[str, Any]:
    fields = _decode_message(payload)
    return {
        "item_id": _first_string(fields, 1), "run_id": _first_string(fields, 2),
        "task_id": _first_string(fields, 3), "sequence": _first_int(fields, 4),
        "kind": _first_string(fields, 5), "state": _first_string(fields, 6),
        "payload_json": _first_message(fields, 7), "payload_size_bytes": _first_int(fields, 8),
        "payload_sha256": _first_string(fields, 9), "parent_item_id": _optional_string(fields, 10),
        "source_call_item_id": _optional_string(fields, 11), "provider_sample_id": _optional_string(fields, 12),
        "call_ordinal": _optional_int(fields, 13), "created_at_ms": _first_int(fields, 14),
        "committed_at_ms": _optional_int(fields, 15),
    }


def _agent_state_response(operation: str, fields: dict[int, list[Any]]) -> dict[str, Any]:
    run_payload = _first_message(fields, 1)
    return {
        "operation": operation,
        "run": _decode_agent_run_record(run_payload) if run_payload else None,
        "items": [_decode_agent_item_record(value) for value in fields.get(2, [])],
        "duplicate": _first_bool(fields, 3, default=False),
        "error": _optional_typed_error(fields, 4),
    }


def _decode_task_node_record(payload: bytes) -> dict[str, Any]:
    fields = _decode_message(payload)
    return {
        "node_id": _first_string(fields, 1),
        "task_id": _first_string(fields, 2),
        "capability_id": _first_string(fields, 3),
        "assigned_instance_id": _optional_string(fields, 4),
        "status": _first_string(fields, 5),
        "input_refs": _all_strings(fields, 11),
        "output_refs": _all_strings(fields, 12),
        "started_at": _optional_string(fields, 13),
        "finished_at": _optional_string(fields, 14),
    }


def _optional_task_node_record(fields: dict[int, list[Any]], field_number: int) -> dict[str, Any] | None:
    payload = _first_message(fields, field_number)
    return _decode_task_node_record(payload) if payload else None


def _ensure_task_identity(
    task: Mapping[str, Any],
    *,
    task_id: str,
    conversation_id: str,
    context: str,
) -> None:
    if task.get("task_id") != task_id or task.get("conversation_id") != conversation_id:
        _raise_task_identity_invalid(f"{context} identity differs from request envelope")


def _raise_task_identity_invalid(message: str) -> None:
    raise RuntimeError(f"runtime_store_response_invalid: {message}")


def _artifact_record(
    *,
    artifact_id: str,
    task_id: str,
    producer_node_id: str,
    artifact_type: str,
    storage_ref: str,
    summary: str,
    is_complete: bool,
    created_at: str,
) -> bytes:
    return b"".join(
        [
            _field_string(1, artifact_id),
            _field_string(2, task_id),
            _field_string(3, producer_node_id),
            _field_string(4, artifact_type),
            _field_string(5, storage_ref),
            _field_string(6, summary),
            _field_varint(7, 1 if is_complete else 0),
            _field_string(8, created_at),
        ]
    )


def _decode_artifact_record(payload: bytes) -> dict[str, Any]:
    fields = _decode_message(payload)
    return {
        "artifact_id": _first_string(fields, 1),
        "task_id": _first_string(fields, 2),
        "producer_node_id": _first_string(fields, 3),
        "artifact_type": _first_string(fields, 4),
        "storage_ref": _first_string(fields, 5),
        "summary": _first_string(fields, 6),
        "is_complete": _first_bool(fields, 7, default=False),
        "created_at": _first_string(fields, 8),
    }


def _artifact_response(operation: str, fields: dict[int, list[Any]]) -> dict[str, Any]:
    artifact_payload = _first_message(fields, 1)
    return {
        "operation": operation,
        "artifact": _decode_artifact_record(artifact_payload) if artifact_payload else None,
        "found": _first_bool(fields, 2, default=bool(artifact_payload)),
        "error": _optional_typed_error(fields, 3),
    }


def _lease_response(operation: str, fields: dict[int, list[Any]]) -> dict[str, Any]:
    return {
        "operation": operation,
        "task_id": _first_string(fields, 1),
        "owner_id": _first_string(fields, 2),
        "revision": _first_int(fields, 3),
        "expires_at_ms": _first_int(fields, 4),
        "renew_token": _first_string(fields, 5),
        "error": _optional_typed_error(fields, 6),
    }


def _bundle_revision_response(operation: str, fields: dict[int, list[Any]]) -> dict[str, Any]:
    return {
        "operation": operation,
        "task_id": _first_string(fields, 1),
        "bundle_kind": _first_string(fields, 2),
        "revision": _first_string(fields, 3),
        "released": _first_bool(fields, 4, default=False),
        "error": _optional_typed_error(fields, 5),
    }


def _optional_typed_error(fields: dict[int, list[Any]], field_number: int) -> dict[str, Any] | None:
    values = fields.get(field_number, [])
    if not values:
        return None
    error_fields = _decode_message(
        values[0],
        allowed_fields=frozenset({1, 2, 3, 4, 5}),
        singular_fields=frozenset({1, 2, 3, 4}),
    )
    safe_metadata: dict[str, str] = {}
    for entry in error_fields.get(5, []):
        entry_fields = _decode_closed_message(entry, 1, 2)
        key = _first_string(entry_fields, 1)
        if key in safe_metadata:
            raise RuntimeError(
                "runtime_store_response_invalid: Rust runtime sidecar returned duplicate safe metadata"
            )
        safe_metadata[key] = _first_string(entry_fields, 2)
    return {
        "code": _first_string(error_fields, 1),
        "message": _first_string(error_fields, 2),
        "retriable": _first_bool(error_fields, 3),
        "category": _category_name(_first_int(error_fields, 4)),
        "safe_metadata": safe_metadata,
    }


def _first_string(fields: dict[int, list[Any]], field_number: int) -> str:
    values = fields.get(field_number, [])
    return bytes(values[0]).decode("utf-8") if values else ""


def _optional_string(fields: dict[int, list[Any]], field_number: int) -> str | None:
    values = fields.get(field_number, [])
    return bytes(values[0]).decode("utf-8") if values else None


def _optional_int(fields: dict[int, list[Any]], field_number: int) -> int | None:
    values = fields.get(field_number, [])
    return int(values[0]) if values else None


def _first_int(fields: dict[int, list[Any]], field_number: int) -> int:
    values = fields.get(field_number, [])
    return int(values[0]) if values else 0


def _category_name(value: int) -> str:
    return {
        1: "configuration",
        2: "compatibility",
        3: "security",
        4: "resource_limit",
        5: "protocol",
        6: "upstream",
        7: "internal",
        8: "cancellation",
    }.get(value, "internal")


def _is_cross_host(hostname: str) -> bool:
    normalized = hostname.strip().lower()
    if normalized == "localhost":
        return False
    try:
        return not ip_address(normalized).is_loopback
    except ValueError:
        return True


__all__ = ["RuntimeSidecarGrpcClient"]

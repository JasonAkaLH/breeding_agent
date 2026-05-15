from __future__ import annotations

import socket
import struct
from collections.abc import Mapping
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

from src.storage.rust_contract import load_runtime_sidecar_contract
from src.storage.runtime_sidecar_facade import (
    validate_runtime_sidecar_config_authority,
    validate_runtime_sidecar_endpoint,
    validate_runtime_sidecar_handshake,
    validate_runtime_sidecar_response,
)


_HTTP2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
_FRAME_DATA = 0x0
_FRAME_HEADERS = 0x1
_FRAME_RST_STREAM = 0x3
_FRAME_SETTINGS = 0x4
_FRAME_GOAWAY = 0x7
_FLAG_END_STREAM = 0x1
_FLAG_END_HEADERS = 0x4
_FLAG_ACK = 0x1


class RuntimeSidecarGrpcClient:
    """Minimal dependency-free gRPC/h2c client for the Rust runtime sidecar.

    The project intentionally has no grpcio dependency yet. This client only
    implements the unary plaintext loopback subset needed by
    `maf-runtime-sidecar` for PRD 03 validation; it is not a general gRPC stack.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        config_source: str = "environment_variable",
        allowed_hosts: tuple[str, ...] = (),
        mtls_enabled: bool = False,
    ) -> None:
        validated_endpoint = validate_runtime_sidecar_endpoint(
            endpoint,
            component="runtime_store",
            unavailable_error_code="runtime_store_unavailable",
            allowed_hosts=allowed_hosts,
        )
        parsed = urlparse(validated_endpoint)
        if parsed.scheme != "http" or not parsed.hostname or not parsed.port:
            raise ValueError("runtime sidecar gRPC endpoint must be http://host:port")
        validate_runtime_sidecar_config_authority(
            "runtime_sidecar_endpoint",
            config_source,
            component="runtime_store",
            cross_host=_is_cross_host(parsed.hostname),
            mtls_enabled=mtls_enabled,
        )
        self._host = parsed.hostname
        self._port = parsed.port
        self._authority = f"{self._host}:{self._port}"

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
        fields = _decode_message(payload)
        response = {
            "operation": "event_append",
            "cursor": _optional_event_cursor(fields, 1),
            "error": _optional_typed_error(fields, 2),
        }
        _consume_response("event_append", response)
        return response["cursor"]

    def submit_task(
        self,
        *,
        task_id: str,
        conversation_id: str,
        idempotency_key: str,
        owner: str = "python-runtime",
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        idempotency = _field_string(1, idempotency_key) + _field_string(2, owner) + _field_varint(3, 0)
        request = _field_string(1, task_id) + _field_string(2, conversation_id) + _field_bytes(3, idempotency)
        payload = self._unary("SubmitTask", request, timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        response = {
            "operation": "task_submit",
            "task_id": _first_string(fields, 1),
            "duplicate": _first_bool(fields, 2, default=False),
            "error": _optional_typed_error(fields, 3),
        }
        _consume_response("task_submit", response)
        return response

    def transition_node(
        self,
        *,
        task_id: str,
        node_id: str,
        to_status: str,
        idempotency_key: str,
        owner: str = "python-runtime",
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self._ensure_compatible(timeout_seconds=timeout_seconds)
        idempotency = _field_string(1, idempotency_key) + _field_string(2, owner) + _field_varint(3, 0)
        request = (
            _field_string(1, task_id)
            + _field_string(2, node_id)
            + _field_string(4, to_status)
            + _field_bytes(5, idempotency)
        )
        payload = self._unary("TransitionNode", request, timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        response = {
            "operation": "node_state_transition",
            "node_id": _first_string(fields, 1),
            "status": _first_string(fields, 2),
            "error": _optional_typed_error(fields, 3),
        }
        _consume_response("node_state_transition", response)
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
        with socket.create_connection((self._host, self._port), timeout=timeout_seconds) as sock:
            sock.settimeout(timeout_seconds)
            sock.sendall(_HTTP2_PREFACE)
            sock.sendall(_frame(_FRAME_SETTINGS, 0, 0, b""))
            header_block = _encode_headers(
                [
                    (":method", "POST"),
                    (":scheme", "http"),
                    (":path", path),
                    (":authority", self._authority),
                    ("content-type", "application/grpc"),
                    ("te", "trailers"),
                    ("user-agent", "maf-runtime-sidecar-client/0.1"),
                ]
            )
            sock.sendall(_frame(_FRAME_HEADERS, _FLAG_END_HEADERS, 1, header_block))
            sock.sendall(_frame(_FRAME_DATA, _FLAG_END_STREAM, 1, grpc_payload))
            return _read_grpc_response(sock)


def _consume_response(operation_name: str, response: Mapping[str, Any]) -> None:
    envelope = validate_runtime_sidecar_response(operation_name, response)
    error = envelope.get("error")
    if isinstance(error, Mapping):
        _raise_typed_error(error)


def _raise_typed_error(error: Mapping[str, Any]) -> None:
    raise RuntimeError(f"{error['code']}: {error['message']}")


def _read_grpc_response(sock: socket.socket) -> bytes:
    data = bytearray()
    while True:
        frame_type, flags, stream_id, payload = _read_frame(sock)
        if frame_type == _FRAME_SETTINGS and flags & _FLAG_ACK == 0:
            sock.sendall(_frame(_FRAME_SETTINGS, _FLAG_ACK, 0, b""))
            continue
        if frame_type == _FRAME_DATA and stream_id == 1:
            data.extend(payload)
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
        return b""
    compressed = data[0]
    if compressed:
        raise RuntimeError("runtime sidecar returned compressed gRPC payload")
    size = struct.unpack(">I", data[1:5])[0]
    return bytes(data[5 : 5 + size])


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


def _decode_message(payload: bytes) -> dict[int, list[Any]]:
    fields: dict[int, list[Any]] = {}
    offset = 0
    while offset < len(payload):
        key, offset = _read_varint(payload, offset)
        field_number = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            value, offset = _read_varint(payload, offset)
        elif wire_type == 2:
            length, offset = _read_varint(payload, offset)
            value = payload[offset : offset + length]
            offset += length
        else:
            raise RuntimeError(f"unsupported protobuf wire type: {wire_type}")
        fields.setdefault(field_number, []).append(value)
    return fields


def _read_varint(payload: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while True:
        byte = payload[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, offset
        shift += 7


def _first_message(fields: dict[int, list[Any]], field_number: int) -> bytes:
    values = fields.get(field_number, [])
    return values[0] if values else b""


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
    error_fields = _decode_message(values[0])
    return {
        "code": _first_string(error_fields, 1),
        "message": _first_string(error_fields, 2),
        "retriable": _first_bool(error_fields, 3),
        "category": _category_name(_first_int(error_fields, 4)),
        "safe_metadata": {},
    }


def _first_string(fields: dict[int, list[Any]], field_number: int) -> str:
    values = fields.get(field_number, [])
    return bytes(values[0]).decode("utf-8") if values else ""


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

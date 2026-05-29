from __future__ import annotations

import socket
import struct
from collections.abc import Iterable
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

from .rust_contract import load_skill_runtime_contract
from .skill_runtime_gates import validate_skill_runtime_artifact_provenance

_HTTP2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
_FRAME_DATA = 0x0
_FRAME_HEADERS = 0x1
_FRAME_RST_STREAM = 0x3
_FRAME_SETTINGS = 0x4
_FRAME_GOAWAY = 0x7
_FLAG_END_STREAM = 0x1
_FLAG_END_HEADERS = 0x4
_FLAG_ACK = 0x1
_MAX_GRPC_RESPONSE_BYTES = 4 * 1024 * 1024
_DEFAULT_CLIENT_VERSION = "0.1.0"


class SkillSandboxGrpcClient:
    """Minimal dependency-free gRPC/h2c client for the Rust Skill Sandbox.

    This is intentionally a narrow unary plaintext loopback client for PRD-04
    validation and local runtime wiring. It is not a general gRPC stack.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        artifact_provenance: dict[str, Any] | None = None,
        allowed_artifact_checksums: tuple[str, ...] = (),
        allowed_cargo_lock_digests: tuple[str, ...] = (),
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or not parsed.hostname or not parsed.port:
            raise RuntimeError("skill_runtime_sandbox_unavailable: Skill sandbox endpoint must be http://host:port")
        if not _is_loopback_host(parsed.hostname):
            raise RuntimeError("skill_runtime_sandbox_unavailable: Skill sandbox endpoint must be loopback")
        if artifact_provenance is not None:
            validate_skill_runtime_artifact_provenance(
                artifact_provenance,
                allowed_checksums=set(allowed_artifact_checksums),
                allowed_cargo_lock_digests=set(allowed_cargo_lock_digests),
            )
        self._host = parsed.hostname
        self._port = parsed.port
        self._authority = f"{self._host}:{self._port}"

    def version(self, *, timeout_seconds: float = 5) -> dict[str, Any]:
        payload = self._unary("Version", b"", timeout_seconds=timeout_seconds)
        version = _decode_version_info(_first_message(_decode_message(payload), 1))
        _validate_handshake(version)
        return version

    def check_compatibility(self, *, timeout_seconds: float = 5) -> dict[str, Any]:
        contract = load_skill_runtime_contract()
        client_version = str(contract.get("client_version") or contract.get("min_client_version") or _DEFAULT_CLIENT_VERSION)
        request = b"".join(
            [
                _field_string(1, client_version),
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
        _validate_handshake(version)
        compatible = _first_bool(fields, 1)
        if not compatible:
            raise RuntimeError("skill_runtime_contract_mismatch: Rust skill sandbox handshake is incompatible")
        return {
            "compatible": True,
            "version": version,
            "missing_features": _all_strings(fields, 3),
            "error": None,
        }

    def execute_sandboxed(
        self,
        *,
        skill_name: str,
        execution_mode: str,
        cwd_under_public_root: str,
        argv: Iterable[str],
        timeout_ms: int,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        stdin_payload: bytes,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self.check_compatibility(timeout_seconds=timeout_seconds)
        request = b"".join(
            [
                _field_string(1, skill_name),
                _field_string(2, execution_mode),
                _field_string(3, cwd_under_public_root),
                _field_varint(4, timeout_ms),
                _field_varint(5, stdout_limit_bytes),
                _field_varint(6, stderr_limit_bytes),
                _field_bytes(7, stdin_payload),
                b"".join(_field_string(8, str(item)) for item in argv),
            ]
        )
        payload = self._unary("ExecuteSandboxed", request, timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        if not any(field_number in fields for field_number in (1, 2, 3, 4, 5, 6)):
            raise RuntimeError("skill_runtime_contract_mismatch: Rust skill sandbox returned empty ExecuteSandboxed response")
        return {
            "exit_code": _first_int32(fields, 1),
            "stdout_prefix": _first_bytes(fields, 2),
            "stderr_prefix": _first_bytes(fields, 3),
            "stdout_truncated": _first_bool(fields, 4),
            "stderr_truncated": _first_bool(fields, 5),
            "error": _optional_typed_error(fields, 6),
        }

    def validate_policy(
        self,
        *,
        skill_name: str,
        capability_id: str,
        execution_mode: str,
        trust_scope: str,
        handler: str,
        manifest_services: Iterable[str],
        runtime_allowlist_services: Iterable[str],
        requested_services: Iterable[str],
        runtime_allowlist_handlers: Iterable[str],
        x_runtime_rust: dict[str, str] | None = None,
        timeout_seconds: float = 5,
    ) -> dict[str, Any]:
        self.check_compatibility(timeout_seconds=timeout_seconds)
        rust_metadata = dict(x_runtime_rust or {})
        request = b"".join(
            [
                _field_string(1, skill_name),
                _field_string(2, capability_id),
                _field_string(3, execution_mode),
                _field_string(4, trust_scope),
                _field_string(5, handler),
                b"".join(_field_string(6, str(service)) for service in manifest_services),
                b"".join(_field_string(7, str(service)) for service in runtime_allowlist_services),
                b"".join(_field_map_entry(8, key, value) for key, value in rust_metadata.items()),
                b"".join(_field_string(9, str(handler_name)) for handler_name in runtime_allowlist_handlers),
                b"".join(_field_string(10, str(service)) for service in requested_services),
            ]
        )
        payload = self._unary("ValidatePolicy", request, timeout_seconds=timeout_seconds)
        fields = _decode_message(payload)
        return {
            "allowed": _first_bool(fields, 1),
            "bundle_fingerprint": _first_string(fields, 2),
            "error": _optional_typed_error(fields, 3),
        }

    def _unary(self, method: str, protobuf_payload: bytes, *, timeout_seconds: float) -> bytes:
        path = f"/maf.skill.v1.SkillSandbox/{method}"
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
                    ("user-agent", "maf-skill-sandbox-client/0.1"),
                ]
            )
            sock.sendall(_frame(_FRAME_HEADERS, _FLAG_END_HEADERS, 1, header_block))
            sock.sendall(_frame(_FRAME_DATA, _FLAG_END_STREAM, 1, grpc_payload))
            return _read_grpc_response(sock)


def _validate_handshake(version: dict[str, Any]) -> None:
    contract = load_skill_runtime_contract()
    expected = {
        "component": contract["component"],
        "protocol_version": contract["protocol_version"],
        "schema_hash": contract["schema_hash"],
        "error_code_table_hash": contract["error_code_table_hash"],
    }
    if any(version.get(key) != value for key, value in expected.items()):
        raise RuntimeError("skill_runtime_contract_mismatch: Rust skill sandbox handshake is incompatible")
    supported = {str(feature) for feature in version.get("supported_features", ())}
    required = {str(feature) for feature in contract["supported_features"]}
    if not required.issubset(supported):
        raise RuntimeError("skill_runtime_contract_mismatch: Rust skill sandbox features are incompatible")
    client_version = str(contract.get("client_version") or contract.get("min_client_version") or _DEFAULT_CLIENT_VERSION)
    min_client_version = str(version.get("min_client_version") or "")
    max_client_version = str(version.get("max_client_version") or "")
    if not min_client_version or not max_client_version:
        raise RuntimeError("skill_runtime_contract_mismatch: Rust skill sandbox did not return client version range")
    if not _version_is_at_least(client_version, min_client_version) or not _version_is_at_most(client_version, max_client_version):
        raise RuntimeError("skill_runtime_contract_mismatch: Rust skill sandbox client version is outside the supported range")


def _raise_typed_error(error: dict[str, Any]) -> None:
    raise RuntimeError(f"{error['code']}: {error['message']}")


def _read_grpc_response(sock: socket.socket) -> bytes:
    data = bytearray()
    while True:
        frame_type, flags, stream_id, payload = _read_frame(sock)
        if frame_type == _FRAME_SETTINGS and flags & _FLAG_ACK == 0:
            sock.sendall(_frame(_FRAME_SETTINGS, _FLAG_ACK, 0, b""))
            continue
        if frame_type == _FRAME_DATA and stream_id == 1:
            if len(data) + len(payload) > _MAX_GRPC_RESPONSE_BYTES:
                raise RuntimeError("skill sandbox gRPC response exceeded configured limit")
            data.extend(payload)
            if flags & _FLAG_END_STREAM:
                break
            continue
        if frame_type == _FRAME_HEADERS and stream_id == 1:
            if flags & _FLAG_END_STREAM:
                break
            continue
        if frame_type == _FRAME_RST_STREAM:
            raise RuntimeError("skill sandbox gRPC stream was reset")
        if frame_type == _FRAME_GOAWAY:
            raise RuntimeError("skill sandbox gRPC connection received GOAWAY")
    if len(data) < 5:
        raise RuntimeError("skill sandbox did not return a complete gRPC message")
    if data[0]:
        raise RuntimeError("skill sandbox returned compressed gRPC payload")
    size = struct.unpack(">I", data[1:5])[0]
    message_end = 5 + size
    if len(data) < message_end:
        raise RuntimeError("skill sandbox returned incomplete gRPC payload")
    if len(data) != message_end:
        raise RuntimeError("skill sandbox returned unexpected trailing gRPC payload bytes")
    return bytes(data[5:message_end])


def _version_is_at_least(version: str, minimum: str) -> bool:
    return _parse_semver(version) >= _parse_semver(minimum)


def _version_is_at_most(version: str, maximum: str) -> bool:
    version_parts = _parse_semver(version)
    max_parts = maximum.split(".")
    if len(max_parts) != 3:
        raise RuntimeError("skill_runtime_contract_mismatch: Rust skill sandbox returned invalid client version range")
    for index, raw_part in enumerate(max_parts[:3]):
        normalized = raw_part.strip().lower()
        if normalized in {"x", "*"}:
            return True
        try:
            max_value = int(normalized)
        except ValueError as exc:
            raise RuntimeError("skill_runtime_contract_mismatch: Rust skill sandbox returned invalid client version range") from exc
        current = version_parts[index] if index < len(version_parts) else 0
        if current < max_value:
            return True
        if current > max_value:
            return False
    return True


def _parse_semver(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    if len(parts) != 3:
        raise RuntimeError("skill_runtime_contract_mismatch: Rust skill sandbox returned invalid client version range")
    try:
        parsed = [int(part) for part in parts]
    except ValueError as exc:
        raise RuntimeError("skill_runtime_contract_mismatch: Rust skill sandbox returned invalid client version range") from exc
    return (parsed[0], parsed[1], parsed[2])


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
            raise RuntimeError("skill sandbox gRPC connection closed unexpectedly")
        chunks.extend(chunk)
    return bytes(chunks)


def _encode_headers(headers: list[tuple[str, str]]) -> bytes:
    encoded = bytearray()
    for name, value in headers:
        name_bytes = name.encode("ascii")
        value_bytes = value.encode("ascii")
        encoded.append(0x00)
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


def _field_map_entry(field_number: int, key: str, value: str) -> bytes:
    return _field_bytes(field_number, _field_string(1, key) + _field_string(2, value))


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
    return _first_bytes(fields, field_number)


def _first_bytes(fields: dict[int, list[Any]], field_number: int) -> bytes:
    values = fields.get(field_number, [])
    return bytes(values[0]) if values else b""


def _all_strings(fields: dict[int, list[Any]], field_number: int) -> list[str]:
    return [bytes(value).decode("utf-8") for value in fields.get(field_number, [])]


def _first_bool(fields: dict[int, list[Any]], field_number: int, *, default: bool = False) -> bool:
    values = fields.get(field_number, [])
    return bool(values[0]) if values else default


def _first_int32(fields: dict[int, list[Any]], field_number: int) -> int:
    values = fields.get(field_number, [])
    if not values:
        return 0
    raw = int(values[0]) & 0xFFFF_FFFF
    return raw - 0x1_0000_0000 if raw >= 0x8000_0000 else raw


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


def _optional_typed_error(fields: dict[int, list[Any]], field_number: int) -> dict[str, Any] | None:
    values = fields.get(field_number, [])
    if not values:
        return None
    error_fields = _decode_message(values[0])
    return {
        "code": _first_string(error_fields, 1),
        "message": _first_string(error_fields, 2),
        "retriable": _first_bool(error_fields, 3),
        "category": _category_name(_first_int32(error_fields, 4)),
        "safe_metadata": {},
    }


def _first_string(fields: dict[int, list[Any]], field_number: int) -> str:
    values = fields.get(field_number, [])
    return bytes(values[0]).decode("utf-8") if values else ""


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


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


__all__ = ["SkillSandboxGrpcClient"]

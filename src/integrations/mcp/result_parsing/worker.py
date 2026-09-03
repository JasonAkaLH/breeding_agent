from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from dataclasses import asdict, dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Literal, Mapping

from .errors import MCPResultParseError
from .json_values import canonical_json_bytes, strict_json_value
from .models import MCPRawResultDescriptor, MCPResultDecodeRequest, MCPResultOutcome
from .projections import build_agent_projection, build_user_view, parsed_result_payload
from .registry import decode_result


PARSER_REVISION = "mcp-result-parser.v2"
CHECKPOINT_SCHEMA = "maf.mcp.validated_result_checkpoint.v1"
MAX_CHECKPOINT_BYTES = 4 * 1024
MAX_WORKER_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
MAX_MAPPING_INPUT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class MCPValidatedResultCheckpoint:
    parser_revision: str
    protocol_version: str
    source: str
    call_ref: str
    raw_sha256: str
    output_schema_sha256: str | None
    outcome: Literal["succeeded", "tool_error", "malformed"]
    parsed_model_sha256: str | None
    diagnostics: tuple[str, ...]
    reason: str | None
    checkpoint_sha256: str


def worker_entry(connection: Connection, job: Mapping[str, Any]) -> None:
    phase = "startup"
    try:
        _apply_worker_limits()
        request = job["request"]
        phase = "decode"
        payload, raw_sha256 = _materialize_payload(request["payload"])
        decode_request = MCPResultDecodeRequest(
            protocol_version=request["protocol_version"],
            source=request["source"],
            payload=payload,
            output_schema=request["output_schema"],
            output_schema_sha256=request["output_schema_sha256"],
            historical_compatibility=request["historical_compatibility"],
        )
        try:
            parsed = decode_result(decode_request)
        except MCPResultParseError as exc:
            checkpoint = _checkpoint(
                request=request,
                raw_sha256=raw_sha256,
                outcome="malformed",
                parsed_model_sha256=None,
                diagnostics=(),
                reason=exc.code,
            )
            connection.send({"checkpoint": asdict(checkpoint)})
            connection.send({"projection": None})
            return
        del decode_request
        del payload
        phase = "model_digest"
        model_sha256 = _streaming_canonical_sha256(parsed_result_payload(parsed))
        outcome = "tool_error" if parsed.outcome is MCPResultOutcome.TOOL_ERROR else "succeeded"
        checkpoint = _checkpoint(
            request=request,
            raw_sha256=raw_sha256,
            outcome=outcome,
            parsed_model_sha256=model_sha256,
            diagnostics=tuple(str(item) for item in parsed.diagnostics),
            reason=None,
        )
        connection.send({"checkpoint": asdict(checkpoint)})
        if outcome != "succeeded":
            connection.send({"projection": None})
            return
        try:
            phase = "projection"
            agent_projection = build_agent_projection(parsed)
            envelope = {
                "schema": "maf.mcp.parsed_result_projection.v2",
                "parsed_model_sha256": model_sha256,
                "user_view": build_user_view(parsed),
                "agent_projection": agent_projection.content,
                "agent_projection_truncated": agent_projection.truncated,
                "workflow_control": None,
            }
            connection.send({"projection": canonical_json_bytes(envelope)})
        except BaseException:
            connection.send({"projection_error": "projection_failed"})
    except BaseException as exc:
        try:
            category = (
                f"resource_{phase}" if isinstance(exc, MemoryError) else "internal_failure"
            )
            connection.send({"worker_error": category})
        except BaseException:
            pass
    finally:
        connection.close()


def _checkpoint(
    *,
    request: Mapping[str, Any],
    raw_sha256: str,
    outcome: Literal["succeeded", "tool_error", "malformed"],
    parsed_model_sha256: str | None,
    diagnostics: tuple[str, ...],
    reason: str | None,
) -> MCPValidatedResultCheckpoint:
    payload = {
        "schema": CHECKPOINT_SCHEMA,
        "parser_revision": PARSER_REVISION,
        "protocol_version": request["protocol_version"],
        "source": request["source"],
        "call_ref": request["call_ref"],
        "raw_sha256": raw_sha256,
        "output_schema_sha256": request["output_schema_sha256"],
        "outcome": outcome,
        "parsed_model_sha256": parsed_model_sha256,
        "diagnostics": list(diagnostics),
        "reason": reason,
    }
    encoded = canonical_json_bytes(payload)
    if len(encoded) > MAX_CHECKPOINT_BYTES:
        raise RuntimeError("checkpoint_too_large")
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return MCPValidatedResultCheckpoint(
        parser_revision=PARSER_REVISION,
        protocol_version=request["protocol_version"],
        source=request["source"],
        call_ref=request["call_ref"],
        raw_sha256=raw_sha256,
        output_schema_sha256=request["output_schema_sha256"],
        outcome=outcome,
        parsed_model_sha256=parsed_model_sha256,
        diagnostics=diagnostics,
        reason=reason,
        checkpoint_sha256=digest,
    )


def _materialize_payload(value: object) -> tuple[object, str]:
    if isinstance(value, MCPRawResultDescriptor):
        parsed = _load_raw_descriptor(value)
        return parsed, "sha256:" + value.sha256.removeprefix("sha256:")
    normalized = strict_json_value(value)
    data = canonical_json_bytes(normalized)
    if len(data) > MAX_MAPPING_INPUT_BYTES:
        raise RuntimeError("mapping_input_exceeds_limit")
    return data, "sha256:" + hashlib.sha256(data).hexdigest()


def _load_raw_descriptor(descriptor: MCPRawResultDescriptor) -> object:
    path = Path(descriptor.path)
    before = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != descriptor.owner_uid
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
        or before.st_size != descriptor.size_bytes
        or before.st_dev != descriptor.device
        or before.st_ino != descriptor.inode
    ):
        raise RuntimeError("raw_descriptor_identity_invalid")
    file_descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(file_descriptor, "rb", closefd=True) as handle:
        opened = os.fstat(handle.fileno())
        if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
            raise RuntimeError("raw_descriptor_identity_changed")
        digest = hashlib.sha256()
        size = 0
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
            size += len(chunk)
        expected = descriptor.sha256.removeprefix("sha256:")
        if size != descriptor.size_bytes or digest.hexdigest() != expected:
            raise RuntimeError("raw_descriptor_digest_invalid")
        handle.seek(0)

        def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise MCPResultParseError("malformed_json")
                result[key] = item
            return result

        text_handle = os.fdopen(os.dup(handle.fileno()), "r", encoding="utf-8", errors="strict")
        try:
            parsed = json.load(
                text_handle,
                object_pairs_hook=pairs_hook,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    MCPResultParseError("malformed_json")
                ),
            )
        finally:
            text_handle.close()
    return strict_json_value(parsed)


def _apply_worker_limits() -> None:
    if sys.platform.startswith("linux"):
        import resource

        resource.setrlimit(
            resource.RLIMIT_AS,
            (MAX_WORKER_ADDRESS_SPACE_BYTES, MAX_WORKER_ADDRESS_SPACE_BYTES),
        )


def _streaming_canonical_sha256(value: object) -> str:
    digest = hashlib.sha256()
    _update_canonical_digest(digest, value)
    return "sha256:" + digest.hexdigest()


def _update_canonical_digest(digest: Any, value: object) -> None:
    def write(text: str) -> None:
        digest.update(text.encode("utf-8"))

    if value is None:
        write("null")
    elif value is True:
        write("true")
    elif value is False:
        write("false")
    elif isinstance(value, (int, float)):
        write(json.dumps(value, allow_nan=False, separators=(",", ":")))
    elif isinstance(value, str):
        write('"')
        for offset in range(0, len(value), 4 * 1024):
            escaped = json.dumps(value[offset : offset + 4 * 1024], ensure_ascii=False)
            write(escaped[1:-1])
        write('"')
    elif isinstance(value, (list, tuple)):
        write("[")
        for index, item in enumerate(value):
            if index:
                write(",")
            _update_canonical_digest(digest, item)
        write("]")
    elif isinstance(value, Mapping):
        write("{")
        for index, key in enumerate(sorted(value)):
            if index:
                write(",")
            _update_canonical_digest(digest, key)
            write(":")
            _update_canonical_digest(digest, value[key])
        write("}")
    else:
        raise TypeError("parsed model contains a non-JSON value")

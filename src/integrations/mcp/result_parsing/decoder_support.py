from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator, Draft7Validator, SchemaError, ValidationError

from .content_blocks import decode_content_blocks
from .errors import MCPResultParseError
from .json_values import canonical_json_bytes
from .models import (
    MCPEmbeddedTextResourceBlock,
    MCPParsedToolResult,
    MCPResultDecodeRequest,
    MCPResultDiagnostic,
    MCPResultOutcome,
    MCPResultSource,
    MCPStructuredContent,
    MCPStructuredSchemaStatus,
    MCPTextResultBlock,
)


def require_source(request: MCPResultDecodeRequest, allowed: frozenset[MCPResultSource]) -> MCPResultSource:
    try:
        source = MCPResultSource(request.source)
    except ValueError as exc:
        raise MCPResultParseError("unsupported_result_source") from exc
    if source not in allowed:
        raise MCPResultParseError("unsupported_result_source")
    return source


def decode_legacy_result(
    request: MCPResultDecodeRequest,
    payload: Mapping[str, Any],
    *,
    allowed_sources: frozenset[MCPResultSource],
    allowed_blocks: frozenset[str],
    allow_annotations: bool,
) -> MCPParsedToolResult:
    source = require_source(request, allowed_sources)
    is_error = _is_error(payload)
    blocks = decode_content_blocks(
        payload.get("content"),
        allowed_kinds=allowed_blocks,
        allow_annotations=allow_annotations,
    )
    return MCPParsedToolResult(
        protocol_version=request.protocol_version,
        source=source,
        outcome=MCPResultOutcome.TOOL_ERROR if is_error else MCPResultOutcome.SUCCEEDED,
        structured_content=MCPStructuredContent(
            present=False,
            schema_status=MCPStructuredSchemaStatus.NOT_SUPPORTED_BY_VERSION,
        ),
        content_blocks=blocks,
        safe_error_code="mcp_tool_error" if is_error else None,
    )


def decode_structured_result(
    request: MCPResultDecodeRequest,
    payload: Mapping[str, Any],
    *,
    allowed_sources: frozenset[MCPResultSource],
    allowed_blocks: frozenset[str],
    structured_must_be_object: bool,
    require_complete_result_type: bool,
) -> MCPParsedToolResult:
    source = require_source(request, allowed_sources)
    diagnostics: list[MCPResultDiagnostic] = []
    if require_complete_result_type:
        result_type = payload.get("resultType")
        missing_legacy = (
            "resultType" not in payload
            and request.historical_compatibility
            and source is MCPResultSource.TASKS_GET
        )
        if result_type != "complete" and not missing_legacy:
            raise MCPResultParseError("result_shape_invalid")
        if missing_legacy:
            diagnostics.append(MCPResultDiagnostic.LEGACY_MISSING_RESULT_TYPE)
    is_error = _is_error(payload)
    blocks = decode_content_blocks(
        payload.get("content"),
        allowed_kinds=allowed_blocks,
        allow_annotations=True,
    )
    present = "structuredContent" in payload
    structured_value = payload.get("structuredContent")
    if present and structured_must_be_object and not isinstance(structured_value, Mapping):
        raise MCPResultParseError("result_shape_invalid")
    if is_error:
        structured = MCPStructuredContent(
            present=present,
            value=structured_value,
            schema_status=MCPStructuredSchemaStatus.NOT_DECLARED,
        )
    else:
        structured = _validate_success_structured(
            request,
            present=present,
            value=structured_value,
        )
        if present and _has_structured_text_duplicate(structured_value, blocks):
            diagnostics.append(MCPResultDiagnostic.STRUCTURED_TEXT_DUPLICATE)
    return MCPParsedToolResult(
        protocol_version=request.protocol_version,
        source=source,
        outcome=MCPResultOutcome.TOOL_ERROR if is_error else MCPResultOutcome.SUCCEEDED,
        structured_content=structured,
        content_blocks=blocks,
        safe_error_code="mcp_tool_error" if is_error else None,
        diagnostics=tuple(diagnostics),
    )


def _is_error(payload: Mapping[str, Any]) -> bool:
    value = payload.get("isError", False)
    if not isinstance(value, bool):
        raise MCPResultParseError("result_shape_invalid")
    return value


def _validate_success_structured(
    request: MCPResultDecodeRequest,
    *,
    present: bool,
    value: object,
) -> MCPStructuredContent:
    schema = request.output_schema
    digest = request.output_schema_sha256
    if (schema is None) != (digest is None):
        raise MCPResultParseError("output_schema_invalid")
    if schema is None:
        return MCPStructuredContent(
            present=present,
            value=value,
            schema_status=MCPStructuredSchemaStatus.NOT_DECLARED,
        )
    schema_bytes = canonical_json_bytes(schema)
    if len(schema_bytes) > 256 * 1024:
        raise MCPResultParseError("output_schema_invalid")
    expected_digest = "sha256:" + hashlib.sha256(schema_bytes).hexdigest()
    if digest != expected_digest:
        raise MCPResultParseError("output_schema_invalid")
    _reject_external_schema_refs(schema)
    if not present:
        raise MCPResultParseError("output_schema_validation_failed")
    dialect = schema.get("$schema")
    if dialect is None or dialect in {
        "https://json-schema.org/draft/2020-12/schema",
        "https://json-schema.org/draft/2020-12/schema#",
    }:
        validator_type = Draft202012Validator
    elif dialect == "http://json-schema.org/draft-07/schema#":
        validator_type = Draft7Validator
    else:
        raise MCPResultParseError("output_schema_invalid")
    try:
        validator_type.check_schema(dict(schema))
        validator_type(dict(schema)).validate(value)
    except SchemaError as exc:
        raise MCPResultParseError("output_schema_invalid") from exc
    except ValidationError as exc:
        raise MCPResultParseError("output_schema_validation_failed") from exc
    return MCPStructuredContent(
        present=True,
        value=value,
        schema_status=MCPStructuredSchemaStatus.VALID,
    )


def _reject_external_schema_refs(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key == "$ref" and (
                not isinstance(nested, str) or not nested.startswith("#")
            ):
                raise MCPResultParseError("output_schema_invalid")
            _reject_external_schema_refs(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_external_schema_refs(nested)


def _has_structured_text_duplicate(value: object, blocks: tuple[object, ...]) -> bool:
    structured = canonical_json_bytes(value)
    for block in blocks:
        if isinstance(block, (MCPTextResultBlock, MCPEmbeddedTextResourceBlock)):
            try:
                candidate = json.loads(block.text.strip())
                if canonical_json_bytes(candidate) == structured:
                    return True
            except (ValueError, MCPResultParseError):
                continue
    return False

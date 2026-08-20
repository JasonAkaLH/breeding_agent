from __future__ import annotations

import base64
import hashlib
import json
import math
import unittest

from src.integrations.mcp.result_parsing import (
    MCPResultDecodeRequest,
    MCPResultDiagnostic,
    MCPResultOutcome,
    MCPResultParseError,
    MCPResultSource,
    build_agent_projection,
    build_user_view,
    decode_result,
)
from src.integrations.mcp.result_parsing.json_values import canonical_json_bytes
from src.integrations.mcp.result_parsing.registry import DECODERS


VERSIONS = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
    "2026-07-28",
)


def _request(
    version: str,
    payload,
    *,
    source: MCPResultSource = MCPResultSource.TOOLS_CALL,
    schema=None,
    historical: bool = False,
) -> MCPResultDecodeRequest:
    digest = None
    if schema is not None:
        digest = "sha256:" + hashlib.sha256(canonical_json_bytes(schema)).hexdigest()
    return MCPResultDecodeRequest(
        protocol_version=version,
        source=source,
        payload=payload,
        output_schema=schema,
        output_schema_sha256=digest,
        historical_compatibility=historical,
    )


def _minimal(version: str, **extra):
    payload = {"content": [{"type": "text", "text": "ok"}], **extra}
    if version == "2026-07-28":
        payload["resultType"] = "complete"
    return payload


class MCPVersionedResultDecoderTest(unittest.TestCase):
    def test_registry_is_closed_to_exactly_five_versions(self) -> None:
        self.assertEqual(tuple(DECODERS), VERSIONS)
        with self.assertRaisesRegex(MCPResultParseError, "unsupported_protocol_version"):
            decode_result(_request("2027-01-01", {"content": []}))

    def test_minimal_text_and_unknown_top_level_extension_work_for_every_version(self) -> None:
        for version in VERSIONS:
            with self.subTest(version=version):
                result = decode_result(
                    _request(version, _minimal(version, vendorExtension={"raw": "ignored"}))
                )
                self.assertEqual(result.outcome, MCPResultOutcome.SUCCEEDED)
                self.assertEqual(result.content_blocks[0].text, "ok")
                self.assertFalse(result.structured_content.present)

    def test_content_and_unknown_blocks_fail_closed(self) -> None:
        for payload in ({}, {"content": "bad"}, {"content": [{"type": "vendor"}]}):
            with self.subTest(payload=payload):
                with self.assertRaises(MCPResultParseError):
                    decode_result(_request("2025-11-25", payload))

    def test_versioned_content_block_matrix_and_binary_projection(self) -> None:
        image = base64.b64encode(b"image bytes").decode()
        audio = base64.b64encode(b"audio bytes").decode()
        result_2024 = decode_result(
            _request(
                "2024-11-05",
                {"content": [{"type": "image", "mimeType": "IMAGE/PNG", "data": image}]},
            )
        )
        self.assertEqual(result_2024.content_blocks[0].mime_type, "image/png")
        with self.assertRaisesRegex(MCPResultParseError, "content_block_invalid"):
            decode_result(
                _request(
                    "2024-11-05",
                    {"content": [{"type": "audio", "mimeType": "audio/wav", "data": audio}]},
                )
            )
        result_2025 = decode_result(
            _request(
                "2025-03-26",
                {"content": [{"type": "audio", "mimeType": "audio/wav", "data": audio}]},
            )
        )
        view = build_user_view(result_2025)
        self.assertEqual(view["content_metadata"][0]["kind"], "audio")
        self.assertNotIn(audio, json.dumps(view))

    def test_embedded_resources_and_resource_links_keep_only_safe_metadata(self) -> None:
        payload = {
            "content": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///private/data.txt",
                        "mimeType": "text/plain",
                        "text": "resource text",
                    },
                },
                {
                    "type": "resource_link",
                    "uri": "https://secret.example/path?q=token",
                    "name": "report",
                    "mimeType": "text/html",
                },
            ]
        }
        result = decode_result(_request("2025-06-18", payload))
        view = build_user_view(result)
        serialized = json.dumps(view)
        self.assertEqual(view["primary"]["text"], "resource text")
        self.assertNotIn("secret.example", serialized)
        self.assertEqual(view["content_metadata"][1]["uri_scheme"], "https")

    def test_pre_2025_06_structured_content_is_ignored_as_extension(self) -> None:
        for version in ("2024-11-05", "2025-03-26"):
            result = decode_result(
                _request(version, _minimal(version, structuredContent={"secret": "raw"}))
            )
            self.assertFalse(result.structured_content.present)
            self.assertNotIn("raw", build_agent_projection(result))

    def test_2025_structured_output_schema_pass_fail_missing_and_tool_error_order(self) -> None:
        schema = {
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "integer"}},
        }
        valid = {"content": [], "structuredContent": {"answer": 3}}
        result = decode_result(_request("2025-06-18", valid, schema=schema))
        self.assertEqual(result.structured_content.schema_status, "valid")
        for invalid in (
            {"content": [], "structuredContent": {"answer": "bad"}},
            {"content": []},
        ):
            with self.assertRaisesRegex(
                MCPResultParseError, "output_schema_validation_failed"
            ):
                decode_result(_request("2025-06-18", invalid, schema=schema))
        tool_error = decode_result(
            _request(
                "2025-06-18",
                {"content": [], "structuredContent": {"answer": "bad"}, "isError": True},
                schema=schema,
            )
        )
        self.assertEqual(tool_error.outcome, MCPResultOutcome.TOOL_ERROR)
        self.assertEqual(tool_error.safe_error_code, "mcp_tool_error")
        external_schema = {"$ref": "https://example.com/result.schema.json"}
        with self.assertRaisesRegex(MCPResultParseError, "output_schema_invalid"):
            decode_result(
                _request("2025-06-18", valid, schema=external_schema)
            )

    def test_2025_structured_content_must_be_object_and_source_matrix_is_closed(self) -> None:
        with self.assertRaisesRegex(MCPResultParseError, "result_shape_invalid"):
            decode_result(
                _request("2025-11-25", {"content": [], "structuredContent": [1]})
            )
        task_result = decode_result(
            _request(
                "2025-11-25",
                {"content": [], "structuredContent": {"answer": 1}},
                source=MCPResultSource.TASKS_RESULT,
            )
        )
        self.assertEqual(task_result.source, MCPResultSource.TASKS_RESULT)
        with self.assertRaisesRegex(MCPResultParseError, "unsupported_result_source"):
            decode_result(
                _request(
                    "2025-11-25",
                    {"content": []},
                    source=MCPResultSource.TASKS_GET,
                )
            )

    def test_2026_supports_every_json_value_and_distinguishes_absent_from_null(self) -> None:
        values = [None, True, 1, 1.5, "value", [1, False], {"answer": 1}]
        for value in values:
            with self.subTest(value=value):
                result = decode_result(
                    _request(
                        "2026-07-28",
                        {"resultType": "complete", "content": [], "structuredContent": value},
                    )
                )
                self.assertTrue(result.structured_content.present)
                self.assertEqual(result.structured_content.value, value)
        absent = decode_result(_request("2026-07-28", _minimal("2026-07-28")))
        self.assertFalse(absent.structured_content.present)

    def test_2026_requires_complete_and_legacy_exception_is_remote_only(self) -> None:
        payload = {"content": [], "structuredContent": None}
        for source, historical in (
            (MCPResultSource.TOOLS_CALL, False),
            (MCPResultSource.TOOLS_CALL, True),
            (MCPResultSource.TASKS_GET, False),
        ):
            with self.subTest(source=source, historical=historical):
                with self.assertRaisesRegex(MCPResultParseError, "result_shape_invalid"):
                    decode_result(
                        _request(
                            "2026-07-28", payload, source=source, historical=historical
                        )
                    )
        result = decode_result(
            _request(
                "2026-07-28",
                payload,
                source=MCPResultSource.TASKS_GET,
                historical=True,
            )
        )
        self.assertIn(MCPResultDiagnostic.LEGACY_MISSING_RESULT_TYPE, result.diagnostics)

    def test_exact_structured_text_duplicate_uses_type_strict_canonical_bytes(self) -> None:
        duplicate = decode_result(
            _request(
                "2025-11-25",
                {
                    "content": [{"type": "text", "text": '{"answer":true}'}],
                    "structuredContent": {"answer": True},
                },
            )
        )
        self.assertIn(MCPResultDiagnostic.STRUCTURED_TEXT_DUPLICATE, duplicate.diagnostics)
        self.assertNotIn("supplemental_texts", build_user_view(duplicate))
        distinct = decode_result(
            _request(
                "2025-11-25",
                {
                    "content": [{"type": "text", "text": '{"answer":1}'}],
                    "structuredContent": {"answer": True},
                },
            )
        )
        self.assertNotIn(MCPResultDiagnostic.STRUCTURED_TEXT_DUPLICATE, distinct.diagnostics)

    def test_strict_json_rejects_duplicate_nan_surrogate_non_string_key_and_non_json(self) -> None:
        payloads = (
            b'{"content":[],"content":[]}',
            b'{"content":[],"value":NaN}',
            b'{"content":[],"value":"\\ud800"}',
            {"content": [], 1: "bad"},
            {"content": [], "value": {1, 2}},
            {"content": [], "value": math.inf},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(MCPResultParseError, "malformed_json"):
                    decode_result(_request("2025-11-25", payload))
        nested = None
        for _ in range(66):
            nested = [nested]
        for payload in (
            {"content": [], "value": {"k" * 1_025: 1}},
            {"content": [], "value": nested},
        ):
            with self.assertRaisesRegex(MCPResultParseError, "malformed_json"):
                decode_result(_request("2025-11-25", payload))
        with self.assertRaisesRegex(MCPResultParseError, "result_shape_invalid"):
            decode_result(
                _request(
                    "2025-11-25",
                    {"content": [{"type": "text", "text": "x"}] * 1_025},
                )
            )

    def test_annotations_are_closed_and_base64_is_strict(self) -> None:
        result = decode_result(
            _request(
                "2025-03-26",
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "ok",
                            "annotations": {
                                "audience": ["user"],
                                "priority": 0.5,
                                "vendor": "ignored",
                            },
                        }
                    ]
                },
            )
        )
        self.assertEqual(result.content_blocks[0].audience, ("user",))
        for annotations in ({"audience": ["system"]}, {"priority": True}, {"priority": 2}):
            with self.subTest(annotations=annotations):
                with self.assertRaisesRegex(MCPResultParseError, "content_block_invalid"):
                    decode_result(
                        _request(
                            "2025-03-26",
                            {"content": [{"type": "text", "text": "x", "annotations": annotations}]},
                        )
                    )
        with self.assertRaisesRegex(MCPResultParseError, "content_block_invalid"):
            decode_result(
                _request(
                    "2025-03-26",
                    {"content": [{"type": "audio", "mimeType": "audio/wav", "data": "%%%"}]},
                )
            )

    def test_projection_redacts_sensitive_keys_urls_and_enforces_budgets(self) -> None:
        result = decode_result(
            _request(
                "2025-11-25",
                {
                    "content": [{"type": "text", "text": "see https://secret.example/a token=abc"}],
                    "structuredContent": {
                        "api_token": "top-secret",
                        "url": "https://secret.example/private",
                        "large": "界" * 30_000,
                    },
                },
            )
        )
        view = build_user_view(result)
        serialized = json.dumps(view, ensure_ascii=False)
        agent = build_agent_projection(result)
        self.assertNotIn("top-secret", serialized + agent)
        self.assertNotIn("secret.example", serialized + agent)
        self.assertLessEqual(len(serialized), 20_000)
        self.assertLessEqual(len(serialized.encode("utf-8")), 80_000)
        self.assertLessEqual(len(agent), 20_000)
        self.assertLessEqual(len(agent.encode("utf-8")), 80_000)
        self.assertTrue(view["projection_truncated"])
        quote_heavy = decode_result(
            _request(
                "2025-11-25",
                {"content": [{"type": "text", "text": '\\\"' * 30_000}]},
            )
        )
        quote_view = build_user_view(quote_heavy)
        quote_json = json.dumps(quote_view, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(quote_json), 20_000)
        self.assertLessEqual(len(quote_json.encode("utf-8")), 80_000)


if __name__ == "__main__":
    unittest.main()

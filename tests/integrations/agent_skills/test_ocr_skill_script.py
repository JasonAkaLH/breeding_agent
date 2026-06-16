from __future__ import annotations

import base64
import io
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_run_ocr_module():
    script_path = Path("skill/ocr/scripts/run_ocr.py").resolve()
    spec = importlib.util.spec_from_file_location("ocr_run_ocr_for_test", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load OCR script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OCRSkillScriptTest(unittest.TestCase):
    def test_artifact_to_file_decodes_base64_binary_upload(self) -> None:
        run_ocr = _load_run_ocr_module()
        png_content = b"\x89PNG\r\n\x1a\nocr-test"

        resolved = run_ocr._artifact_to_file(
            {
                "filename": "scan.png",
                "content_type": "image/png",
                "encoding": "base64",
                "content_base64": base64.b64encode(png_content).decode("ascii"),
            }
        )

        self.assertEqual(resolved, (png_content, "scan.png", "image/png"))

    def test_read_config_uses_skill_local_config_file(self) -> None:
        run_ocr = _load_run_ocr_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
base_url: http://ocr.example.test/
auth_token: test-token
timeout_seconds: 123
poll_interval_seconds: 0.5
debug_progress: true
""".strip(),
                encoding="utf-8",
            )
            with patch.object(run_ocr, "OCR_CONFIG_PATH", config_path):
                config = run_ocr._read_config({"metadata": {"ocr_mcp_base_url": "http://ignored.example.test"}})

        self.assertEqual(config["base_url"], "http://ocr.example.test")
        self.assertEqual(config["token"], "test-token")
        self.assertEqual(config["timeout_seconds"], 123)
        self.assertEqual(config["poll_interval_seconds"], 0.5)
        self.assertTrue(config["debug_progress"])

    def test_read_config_accepts_nested_ocr_mcp_mapping(self) -> None:
        run_ocr = _load_run_ocr_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
ocr_mcp:
  base_url: http://ocr-from-skill-config.example.test
  auth_token: nested-token
""".strip(),
                encoding="utf-8",
            )
            with patch.object(run_ocr, "OCR_CONFIG_PATH", config_path):
                config = run_ocr._read_config({})

        self.assertEqual(config["base_url"], "http://ocr-from-skill-config.example.test")
        self.assertEqual(config["token"], "nested-token")
        self.assertEqual(config["timeout_seconds"], 3600)
        self.assertEqual(config["poll_interval_seconds"], 2.0)
        self.assertFalse(config["debug_progress"])

    def test_read_config_rejects_non_mapping_skill_config(self) -> None:
        run_ocr = _load_run_ocr_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("- invalid\n", encoding="utf-8")
            with patch.object(run_ocr, "OCR_CONFIG_PATH", config_path):
                with self.assertRaises(run_ocr.OCRSkillError) as context:
                    run_ocr._read_config({})

        self.assertEqual(context.exception.error_code, "ocr_mcp_config_invalid")
        self.assertEqual(context.exception.stage, "config")

    def test_progress_logging_is_opt_in_and_redacts_sensitive_values(self) -> None:
        run_ocr = _load_run_ocr_module()
        stderr = io.StringIO()

        run_ocr.PROGRESS_ENABLED = True
        with patch.object(sys, "stderr", stderr):
            run_ocr._progress(
                "diagnostic",
                token="SECRET_TOKEN_12345678",
                authorization="Bearer SECRET_TOKEN_12345678",
                detail="token=SECRET_TOKEN_12345678",
            )

        run_ocr.PROGRESS_ENABLED = False
        output = stderr.getvalue()
        self.assertIn("OCR_PROGRESS", output)
        self.assertIn("[REDACTED]", output)
        self.assertNotIn("SECRET_TOKEN_12345678", output)

    def test_wait_for_result_retries_transient_http_error_during_polling(self) -> None:
        run_ocr = _load_run_ocr_module()
        responses = [
            RuntimeError("HTTP 502:"),
            {
                "structuredContent": {
                    "status": "succeeded",
                    "markdown": "识别完成",
                    "result": {"pages": []},
                }
            },
        ]

        def fake_call_tool(*_args, **_kwargs):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        with (
            patch.object(run_ocr, "_call_tool", side_effect=fake_call_tool),
            patch.object(run_ocr.time, "sleep", return_value=None),
        ):
            result = run_ocr._wait_for_result(
                {"timeout_seconds": 5, "poll_interval_seconds": 0.01},
                "session-id",
                "job-id",
            )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["markdown"], "识别完成")
        self.assertEqual(responses, [])

    def test_wait_for_result_continues_on_result_not_ready_tool_error(self) -> None:
        run_ocr = _load_run_ocr_module()
        responses = [
            {"isError": True, "structuredContent": {"error": {"code": "RESULT_NOT_READY"}}},
            {
                "structuredContent": {
                    "status": "succeeded",
                    "markdown": "识别完成",
                    "result": {"pages": []},
                }
            },
        ]

        def fake_call_tool(*_args, **_kwargs):
            return responses.pop(0)

        with (
            patch.object(run_ocr, "_call_tool", side_effect=fake_call_tool),
            patch.object(run_ocr.time, "sleep", return_value=None),
        ):
            result = run_ocr._wait_for_result(
                {"timeout_seconds": 5, "poll_interval_seconds": 0.01},
                "session-id",
                "job-id",
            )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["markdown"], "识别完成")
        self.assertEqual(responses, [])

    def test_main_returns_raw_ocr_text_display_artifact_on_success(self) -> None:
        run_ocr = _load_run_ocr_module()
        png_content = b"\x89PNG\r\n\x1a\nocr-test"
        payload = {
            "query": "识别图片",
            "uploaded_artifacts": [
                {
                    "filename": "scan.png",
                    "content_type": "image/png",
                    "encoding": "base64",
                    "content_base64": base64.b64encode(png_content).decode("ascii"),
                }
            ],
        }

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("base_url: http://ocr.example.test\n", encoding="utf-8")
            with (
                patch.object(run_ocr, "OCR_CONFIG_PATH", config_path),
                patch.object(run_ocr, "_upload", return_value="upload-id"),
                patch.object(run_ocr, "_initialize", return_value="session-id"),
                patch.object(run_ocr, "_start_parse_job", return_value="job-id"),
                patch.object(
                    run_ocr,
                    "_wait_for_result",
                    return_value={
                        "status": "succeeded",
                        "markdown": "品种：龙粳33\n处理：A1",
                        "result": {"pages": [{"text": "品种：龙粳33"}]},
                    },
                ),
                patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                patch.object(sys, "stdout", stdout),
            ):
                run_ocr.main()

        result = json.loads(stdout.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "品种：龙粳33\n处理：A1")
        artifacts = result["display_artifacts"]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["artifact_role"], "ocr_raw_text")
        self.assertEqual(artifacts[0]["storage_ref"]["domain_kind"], "ocr")
        self.assertEqual(artifacts[0]["storage_ref"]["raw_text"], "品种：龙粳33\n处理：A1")
        self.assertEqual(artifacts[0]["storage_ref"]["filename"], "scan.png")

    def test_main_returns_structured_error_when_remote_ocr_connection_fails(self) -> None:
        run_ocr = _load_run_ocr_module()
        png_content = b"\x89PNG\r\n\x1a\nocr-test"
        payload = {
            "query": "识别图片",
            "metadata": {"ocr_mcp_base_url": "http://ocr.example.test"},
            "uploaded_artifacts": [
                {
                    "filename": "scan.png",
                    "content_type": "image/png",
                    "encoding": "base64",
                    "content_base64": base64.b64encode(png_content).decode("ascii"),
                }
            ],
        }

        def fail_upload(*_args, **_kwargs):
            raise RuntimeError("连接 OCR MCP 失败：Authorization: Bearer SECRET_TOKEN timed out")

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("base_url: http://ocr.example.test\nauth_token: SECRET_TOKEN\n", encoding="utf-8")
            with (
                patch.object(run_ocr, "OCR_CONFIG_PATH", config_path),
                patch.object(run_ocr, "_upload", side_effect=fail_upload),
                patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                patch.object(sys, "stdout", stdout),
            ):
                run_ocr.main()

        result = json.loads(stdout.getvalue())
        self.assertFalse(result["ok"])
        self.assertIs(result["is_error"], True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "ocr_mcp_connection_failed")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertEqual(result["stage"], "upload")
        self.assertTrue(result["retriable"])
        self.assertIn("OCR 失败：连接 OCR MCP 失败", result["answer"])
        self.assertNotIn("SECRET_TOKEN", json.dumps(result, ensure_ascii=False))

    def test_main_returns_missing_input_when_ocr_file_is_missing(self) -> None:
        run_ocr = _load_run_ocr_module()
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("base_url: http://ocr.example.test\n", encoding="utf-8")
            with (
                patch.object(run_ocr, "OCR_CONFIG_PATH", config_path),
                patch.object(sys, "stdin", io.StringIO(json.dumps({"query": "识别图片"}))),
                patch.object(sys, "stdout", stdout),
            ):
                run_ocr.main()

        result = json.loads(stdout.getvalue())
        self.assertFalse(result["ok"])
        self.assertIs(result["is_error"], True)
        self.assertEqual(result["error"]["type"], "missing_input")
        self.assertEqual(result["error_code"], "ocr_input_missing")
        self.assertEqual(result["error_type"], "missing_input")
        self.assertEqual(result["stage"], "input")
        self.assertEqual(result["status"], "missing_input")
        self.assertIn("document", result["missing"])
        self.assertIn("请上传图片/PDF", result["answer"])

    def test_main_returns_structured_error_when_ocr_base_url_is_missing(self) -> None:
        run_ocr = _load_run_ocr_module()
        png_content = b"\x89PNG\r\n\x1a\nocr-test"
        payload = {
            "query": "识别图片",
            "uploaded_artifacts": [
                {
                    "filename": "scan.png",
                    "content_type": "image/png",
                    "encoding": "base64",
                    "content_base64": base64.b64encode(png_content).decode("ascii"),
                }
            ],
        }
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_config_path = Path(tmpdir) / "missing-config.yaml"
            with (
                patch.object(run_ocr, "OCR_CONFIG_PATH", missing_config_path),
                patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                patch.object(sys, "stdout", stdout),
            ):
                run_ocr.main()

        result = json.loads(stdout.getvalue())
        self.assertFalse(result["ok"])
        self.assertIs(result["is_error"], True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "ocr_mcp_config_missing")
        self.assertEqual(result["error_type"], "ValueError")
        self.assertEqual(result["stage"], "config")
        self.assertFalse(result["retriable"])


if __name__ == "__main__":
    unittest.main()

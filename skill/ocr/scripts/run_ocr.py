from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OCR_MCP_BASE_URL = ""
OCR_MCP_AUTH_TOKEN = ""
DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_DEBUG_PROGRESS = False
PROGRESS_ENABLED = False
PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf"}
TRANSIENT_POLL_HTTP_STATUS_PREFIXES = ("HTTP 408", "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")


class OCRSkillError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "ocr_failed",
        stage: str = "unknown",
        retriable: bool = False,
        error_type: str = "RuntimeError",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.stage = stage
        self.retriable = retriable
        self.error_type = error_type


def main() -> None:
    payload = _read_payload()
    try:
        config = _read_config(payload)
        _set_progress_enabled(config)
        _progress(
            "config_loaded",
            base_url=config["base_url"],
            timeout_seconds=config["timeout_seconds"],
            poll_interval_seconds=config["poll_interval_seconds"],
            auth_configured=bool(config["token"]),
        )
        file_bytes, filename, mime_type = _resolve_input_file(payload)
        _progress("input_resolved", filename=filename, mime_type=mime_type, size_bytes=len(file_bytes))
        upload_id = _run_stage("upload", True, _upload, config, file_bytes, filename, mime_type)
        _progress("upload_done", upload_id=upload_id, filename=filename)
        session_id = _run_stage("initialize", True, _initialize, config)
        _progress("mcp_initialized")
        job_id = _run_stage("start_parse_job", True, _start_parse_job, config, session_id, upload_id)
        _progress("job_started", job_id=job_id)
        result_payload = _run_stage("get_parse_job", True, _wait_for_result, config, session_id, job_id)
        _progress(
            "result_received",
            job_id=job_id,
            status=result_payload.get("status", "succeeded"),
            has_markdown=bool(result_payload.get("markdown")),
            has_structured_result=bool(result_payload.get("result")),
            has_receipt=bool(result_payload.get("result_receipt")),
        )
        receipt = result_payload.get("result_receipt")
        if receipt:
            _progress("ack_start", job_id=job_id)
            _run_stage("ack_parse_job", False, _ack, config, session_id, job_id, receipt)
            _progress("ack_done", job_id=job_id)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        output_format = str(payload.get("output_format") or metadata.get("output_format") or "markdown").lower()
        answer = _format_answer(result_payload, output_format)
        raw_text = _raw_ocr_text(result_payload, answer)
        _progress("done", job_id=job_id, ok=True)
        _print_json(
            {
                "ok": True,
                "answer": answer,
                "content": raw_text,
                "job_id": job_id,
                "filename": filename,
                "mime_type": mime_type,
                "markdown": result_payload.get("markdown"),
                "structured_result": result_payload.get("result"),
                "status": result_payload.get("status", "succeeded"),
                "display_artifacts": [
                    _ocr_raw_text_artifact(
                        raw_text=raw_text,
                        filename=filename,
                        mime_type=mime_type,
                        job_id=job_id,
                        status=str(result_payload.get("status", "succeeded")),
                    )
                ],
            }
        )
    except Exception as exc:  # keep stdout JSON object for Skill runner
        error = _coerce_ocr_error(exc)
        error_message = _redact_error_text(str(error))
        _progress(
            "failed",
            stage=error.stage,
            error_code=error.error_code,
            error_type=error.error_type,
            retriable=error.retriable,
            error=error_message,
        )
        _print_json(
            {
                "ok": False,
                "answer": f"OCR 失败：{error_message}",
                "error": error_message,
                "error_code": error.error_code,
                "error_type": error.error_type,
                "stage": error.stage,
                "retriable": error.retriable,
                "status": "failed",
            }
        )


def _read_payload() -> dict[str, Any]:
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def _read_config(payload: dict[str, Any]) -> dict[str, Any]:
    del payload  # Service connection and runtime limits are intentionally fixed in code.
    base_url = _config_value("OCR_MCP_BASE_URL", "MAF_CONFIG_OCR_MCP__BASE_URL", default=OCR_MCP_BASE_URL)
    auth_token = _config_value("OCR_MCP_AUTH_TOKEN", "MAF_CONFIG_OCR_MCP__AUTH_TOKEN", default=OCR_MCP_AUTH_TOKEN)
    normalized_base_url = base_url.strip().rstrip("/")
    if not normalized_base_url:
        raise OCRSkillError(
            "缺少 OCR MCP 服务地址，请通过本地 config.yaml 的 ocr_mcp.base_url 或 OCR_MCP_BASE_URL 配置。",
            error_code="ocr_mcp_config_missing",
            stage="config",
            retriable=False,
            error_type="ValueError",
        )
    return {
        "base_url": normalized_base_url,
        "token": auth_token,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "poll_interval_seconds": DEFAULT_POLL_INTERVAL_SECONDS,
        "debug_progress": DEFAULT_DEBUG_PROGRESS,
    }


def _config_value(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    return default


def _set_progress_enabled(config: dict[str, Any]) -> None:
    global PROGRESS_ENABLED
    PROGRESS_ENABLED = bool(config.get("debug_progress"))


def _run_stage(stage: str, retriable: bool, func: Callable[..., Any], *args: Any) -> Any:
    try:
        return func(*args)
    except OCRSkillError:
        raise
    except Exception as exc:
        raise OCRSkillError(
            str(exc),
            error_code=_classify_error_code(stage, exc),
            stage=stage,
            retriable=retriable,
            error_type=type(exc).__name__,
        ) from exc


def _coerce_ocr_error(exc: Exception) -> OCRSkillError:
    if isinstance(exc, OCRSkillError):
        return exc
    return OCRSkillError(str(exc), error_type=type(exc).__name__)


def _redact_error_text(value: str) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(token\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(cookie\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", text)
    return text or "OCR 失败"


def _progress(event: str, **fields: Any) -> None:
    if not PROGRESS_ENABLED:
        return
    payload = {
        "event": event,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    payload.update(fields)
    print(
        "OCR_PROGRESS " + json.dumps(_redact_progress_payload(payload), ensure_ascii=False, default=str),
        file=sys.stderr,
        flush=True,
    )


def _redact_progress_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(secret_key in lowered for secret_key in {"token", "authorization", "cookie", "secret", "receipt"}):
                redacted[str(key)] = "[REDACTED]" if item else item
            else:
                redacted[str(key)] = _redact_progress_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_progress_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_error_text(value)
    return value


def _classify_error_code(stage: str, exc: Exception) -> str:
    message = str(exc)
    if "连接 OCR MCP 失败" in message:
        return "ocr_mcp_connection_failed"
    if message.startswith("HTTP ") or "HTTP " in message:
        return "ocr_mcp_http_error"
    if "超时" in message or "timed out" in message.lower() or "timeout" in message.lower():
        return "ocr_timeout"
    if "MCP" in message:
        return f"ocr_mcp_{stage}_failed"
    if stage == "upload":
        return "ocr_upload_failed"
    return f"ocr_{stage}_failed"


def _resolve_input_file(payload: dict[str, Any]) -> tuple[bytes, str, str]:
    file_path = payload.get("file_path") or _extract_path_from_query(str(payload.get("query") or ""))
    if file_path:
        path = Path(str(file_path)).expanduser()
        if not path.is_file():
            raise RuntimeError(f"找不到文件：{path}")
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise RuntimeError("仅支持 PNG、JPG/JPEG、PDF")
        data = path.read_bytes()
        return data, path.name, _guess_mime(path.name)

    artifacts = payload.get("uploaded_artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            resolved = _artifact_to_file(artifact)
            if resolved:
                return resolved

    raise RuntimeError("缺少 OCR 输入文件。请上传图片/PDF，或提供 file_path。")


def _extract_path_from_query(query: str) -> str | None:
    patterns = [
        r"(?:file_path|path|文件路径|图片路径|PDF路径)\s*[:：=]\s*([^\s]+)",
        r"([~/\.A-Za-z0-9_\-/]+\.(?:png|jpg|jpeg|pdf))",
    ]
    for pattern in patterns:
        match = re.search(pattern, query, re.IGNORECASE)
        if match:
            return match.group(1).strip().strip('"\'')
    return None


def _artifact_to_file(artifact: dict[str, Any]) -> tuple[bytes, str, str] | None:
    filename = str(artifact.get("filename") or artifact.get("name") or "upload.bin")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        file_type = str(artifact.get("file_type") or artifact.get("mime_type") or artifact.get("content_type") or "").lower()
        if "pdf" in file_type:
            filename = filename if filename.lower().endswith(".pdf") else filename + ".pdf"
        elif "png" in file_type:
            filename = filename if filename.lower().endswith(".png") else filename + ".png"
        elif "jpeg" in file_type or "jpg" in file_type:
            filename = filename if Path(filename).suffix.lower() in {".jpg", ".jpeg"} else filename + ".jpg"
        else:
            return None

    data: bytes | None = None
    if artifact.get("content_base64"):
        data = base64.b64decode(str(artifact["content_base64"]), validate=False)
    elif artifact.get("data_base64"):
        data = base64.b64decode(str(artifact["data_base64"]), validate=False)
    elif artifact.get("encoding") == "base64" and artifact.get("content"):
        data = base64.b64decode(str(artifact["content"]), validate=False)
    elif artifact.get("content") is not None:
        content = artifact.get("content")
        data = content if isinstance(content, bytes) else str(content).encode("utf-8")
    elif artifact.get("path"):
        path = Path(str(artifact["path"])).expanduser()
        if path.is_file():
            data = path.read_bytes()
            filename = path.name

    if data is None:
        return None
    return data, Path(filename).name, _guess_mime(filename)


def _guess_mime(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".pdf":
        return "application/pdf"
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _upload(config: dict[str, Any], file_bytes: bytes, filename: str, mime_type: str) -> str:
    boundary = "----ocrmcp-" + uuid.uuid4().hex
    body = _multipart_body(boundary, file_bytes, filename, mime_type)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    if config["token"]:
        headers["Authorization"] = f"Bearer {config['token']}"
    data = _http_json(config["base_url"] + "/uploads", "POST", headers, body, timeout=60)
    upload_id = data.get("upload_id")
    if not upload_id:
        raise RuntimeError(f"上传失败：{data}")
    return str(upload_id)


def _multipart_body(boundary: str, file_bytes: bytes, filename: str, mime_type: str) -> bytes:
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return head + file_bytes + tail


def _initialize(config: dict[str, Any]) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "ocr-skill", "version": "0.1"},
        },
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _mcp_headers(config)
    response, response_headers = _http_json_with_headers(config["base_url"] + "/mcp", "POST", headers, body, timeout=30)
    if response.get("error"):
        raise RuntimeError(f"MCP initialize 失败：{response['error']}")
    session_id = response_headers.get("Mcp-Session-Id") or response_headers.get("MCP-Session-Id") or response_headers.get("mcp-session-id")
    if not session_id:
        raise RuntimeError("MCP initialize 未返回 MCP-Session-Id")
    return session_id


def _start_parse_job(config: dict[str, Any], session_id: str, upload_id: str) -> str:
    result = _call_tool(config, session_id, 2, "start_parse_job", {
        "source": {"type": "upload_id", "upload_id": upload_id},
        "result_format": "both",
        "return_markdown": True,
    })
    content = _structured(result)
    job_id = content.get("job_id")
    if not job_id:
        raise RuntimeError(f"start_parse_job 未返回 job_id：{content}")
    return str(job_id)


def _wait_for_result(config: dict[str, Any], session_id: str, job_id: str) -> dict[str, Any]:
    deadline = time.time() + int(config["timeout_seconds"])
    cursor = None
    chunks: list[bytes] = []
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        args: dict[str, Any] = {"job_id": job_id, "include_result": True, "result_format": "both"}
        if cursor:
            args["cursor"] = cursor
        try:
            result = _call_tool(config, session_id, 3, "get_parse_job", args)
        except Exception as exc:
            if _is_transient_poll_error(exc) and time.time() < deadline:
                _progress(
                    "job_poll_transient_error",
                    job_id=job_id,
                    attempt=attempt,
                    error=str(exc),
                )
                time.sleep(float(config["poll_interval_seconds"]))
                continue
            raise
        content = _structured(result)
        if result.get("isError"):
            error_payload = content.get("error") if isinstance(content.get("error"), dict) else content
            if isinstance(error_payload, dict):
                error_code = str(error_payload.get("code") or content.get("code") or "").upper()
            else:
                error_code = ""
            if error_code == "RESULT_NOT_READY":
                _progress("job_not_ready", job_id=job_id, attempt=attempt)
                time.sleep(float(config["poll_interval_seconds"]))
                continue
            raise RuntimeError(f"get_parse_job 返回错误：{content}")
        status = content.get("status")
        _progress(
            "job_polled",
            job_id=job_id,
            attempt=attempt,
            status=status,
            cursor=bool(cursor),
            has_result=bool(content.get("result") or content.get("markdown")),
            has_chunk=bool(content.get("result_chunk")),
        )
        if status in {"failed", "cancelled", "expired", "gone"}:
            _progress("job_terminal_error", job_id=job_id, status=status, error=content.get("error"))
            raise RuntimeError(f"OCR job {status}：{content.get('error') or content}")
        if status == "succeeded":
            if "result_chunk" in content:
                chunk = content["result_chunk"]
                if chunk.get("encoding") != "base64url":
                    raise RuntimeError("未知结果分块编码")
                chunks.append(_b64url_decode(str(chunk.get("payload") or "")))
                if content.get("is_final_chunk"):
                    merged = json.loads(b"".join(chunks).decode("utf-8"))
                    merged["status"] = status
                    if content.get("result_receipt"):
                        merged["result_receipt"] = content["result_receipt"]
                    _progress(
                        "result_chunked_done",
                        job_id=job_id,
                        chunks=len(chunks),
                        has_receipt=bool(content.get("result_receipt")),
                    )
                    return merged
                cursor = content.get("next_cursor")
                if not cursor:
                    raise RuntimeError("结果分块缺少 next_cursor")
                continue
            _progress(
                "result_inline_done",
                job_id=job_id,
                has_markdown=bool(content.get("markdown")),
                has_structured_result=bool(content.get("result")),
                has_receipt=bool(content.get("result_receipt")),
            )
            return content
        time.sleep(float(config["poll_interval_seconds"]))
    _progress("timeout", job_id=job_id, timeout_seconds=config["timeout_seconds"])
    raise RuntimeError(f"OCR 超时，job_id={job_id}")


def _ack(config: dict[str, Any], session_id: str, job_id: str, receipt: str) -> None:
    result = _call_tool(config, session_id, 4, "ack_parse_job", {"job_id": job_id, "result_receipt": receipt})
    if result.get("isError"):
        raise RuntimeError(f"ack_parse_job 失败：{_structured(result)}")


def _call_tool(config: dict[str, Any], session_id: str, rpc_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    headers = _mcp_headers(config)
    headers["MCP-Session-Id"] = session_id
    response = _http_json(config["base_url"] + "/mcp", "POST", headers, json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=60)
    if response.get("error"):
        raise RuntimeError(f"MCP {name} 失败：{response['error']}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"MCP {name} 返回异常：{response}")
    return result


def _mcp_headers(config: dict[str, Any]) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if config["token"]:
        headers["Authorization"] = f"Bearer {config['token']}"
    return headers


def _http_json(
    url: str,
    method: str,
    headers: dict[str, str],
    body: bytes | None = None,
    timeout: int | float = 30,
) -> dict[str, Any]:
    response, _ = _http_json_with_headers(url, method, headers, body, timeout)
    return response


def _http_json_with_headers(
    url: str,
    method: str,
    headers: dict[str, str],
    body: bytes | None = None,
    timeout: int | float = 30,
) -> tuple[dict[str, Any], Any]:
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            data = json.loads(raw.decode("utf-8") or "{}") if raw else {}
            return data, resp.headers
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw)
        except Exception:
            detail = raw
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"连接 OCR MCP 失败：{exc.reason}") from exc



def _is_transient_poll_error(exc: Exception) -> bool:
    message = str(exc).strip()
    return any(message.startswith(prefix) for prefix in TRANSIENT_POLL_HTTP_STATUS_PREFIXES)


def _structured(tool_result: dict[str, Any]) -> dict[str, Any]:
    content = tool_result.get("structuredContent")
    return content if isinstance(content, dict) else {}


def _format_answer(result_payload: dict[str, Any], output_format: str) -> str:
    markdown = result_payload.get("markdown")
    structured = result_payload.get("result")
    if output_format == "json":
        return json.dumps(structured or result_payload, ensure_ascii=False, indent=2)
    if output_format == "both":
        return (markdown or "") + "\n\n```json\n" + json.dumps(structured or {}, ensure_ascii=False, indent=2) + "\n```"
    if markdown:
        return str(markdown)
    if structured:
        return json.dumps(structured, ensure_ascii=False, indent=2)
    return json.dumps(result_payload, ensure_ascii=False, indent=2)


def _raw_ocr_text(result_payload: dict[str, Any], fallback: str) -> str:
    markdown = result_payload.get("markdown")
    if isinstance(markdown, str) and markdown.strip():
        return markdown
    answer = result_payload.get("answer")
    if isinstance(answer, str) and answer.strip():
        return answer
    structured = result_payload.get("result")
    if structured:
        return json.dumps(structured, ensure_ascii=False, indent=2)
    return fallback


def _ocr_raw_text_artifact(
    *,
    raw_text: str,
    filename: str,
    mime_type: str,
    job_id: str,
    status: str,
) -> dict[str, Any]:
    return {
        "artifact_type": "json",
        "artifact_role": "ocr_raw_text",
        "artifact_id_suffix": "ocr_raw_text",
        "summary": f"OCR 回传原文：{filename}",
        "storage_ref": {
            "domain_kind": "ocr",
            "artifact_role": "ocr_raw_text",
            "raw_text": raw_text,
            "filename": filename,
            "mime_type": mime_type,
            "job_id": job_id,
            "status": status,
        },
    }


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))





def _print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()

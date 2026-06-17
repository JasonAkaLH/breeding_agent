from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Mapping


DEFAULT_IMGPU_URL = "http://imgpu:8000"
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
CROP_ALIASES = {
    "maize": "maize",
    "corn": "maize",
    "玉米": "maize",
    "rice": "rice",
    "水稻": "rice",
    "稻": "rice",
}
CROP_LABELS = {
    "maize": "玉米",
    "rice": "水稻",
}


def emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))


def fail(answer: str, *, missing: list[str] | None = None, error_type: str = "plant_dis_error") -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "is_error": True,
        "answer": answer,
        "response_text": answer,
        "error": {"type": error_type, "message": answer},
    }
    if missing:
        result["missing"] = missing
    return result


def imgpu_base_url() -> str:
    return (os.getenv("PLANT_DIS_IMGPU_URL") or os.getenv("IMGPU_BASE_URL") or DEFAULT_IMGPU_URL).rstrip("/")


def normalize_crop(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in CROP_ALIASES:
        return CROP_ALIASES[text]
    for key, crop in CROP_ALIASES.items():
        if re.search(rf"(?<![A-Za-z]){re.escape(key)}(?![A-Za-z])", text, flags=re.IGNORECASE):
            return crop
    return None


def safe_image_name(value: Any, default: str = "plant_image.jpg") -> str:
    name = Path(str(value or default)).name.replace("\x00", "")
    if not name or name in {".", ".."}:
        name = default
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        name = f"{Path(name).stem or 'plant_image'}.jpg"
    return name


def decode_artifact_content(artifact: Mapping[str, Any]) -> bytes | None:
    raw = artifact.get("content")
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        return raw.encode("utf-8")
    encoded = artifact.get("content_base64") or artifact.get("base64")
    if isinstance(encoded, str):
        value = encoded.strip()
        if "," in value and value.lower().startswith("data:"):
            value = value.split(",", 1)[1]
        try:
            return base64.b64decode(value, validate=True)
        except Exception:
            return None
    return None


def artifact_path(artifact: Mapping[str, Any]) -> Path | None:
    for key in ("path", "file_path", "local_path", "tmp_path"):
        raw = artifact.get(key)
        if isinstance(raw, str) and raw.strip():
            candidate = Path(raw).expanduser()
            if candidate.exists() and candidate.is_file():
                return candidate.resolve()
    return None


def write_artifact(artifact: Mapping[str, Any], work_dir: Path) -> Path | None:
    existing = artifact_path(artifact)
    if existing is not None:
        return existing
    content = decode_artifact_content(artifact)
    if content is None:
        return None
    name = safe_image_name(artifact.get("filename") or artifact.get("name"))
    output = work_dir / name
    output.write_bytes(content)
    return output


def image_from_resource_manifest(payload: Mapping[str, Any]) -> Path | None:
    raw = payload.get("resource_manifest_path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    manifest_path = Path(raw).expanduser()
    if not manifest_path.exists() or not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    files = manifest.get("files") if isinstance(manifest, Mapping) else None
    if not isinstance(files, list):
        return None
    for item in files:
        if not isinstance(item, Mapping):
            continue
        mount_path = item.get("mount_path")
        if not isinstance(mount_path, str) or not mount_path.strip():
            continue
        candidate = Path(mount_path).expanduser()
        if candidate.exists() and candidate.is_file() and candidate.suffix.lower() in SUPPORTED_EXTENSIONS:
            return candidate.resolve()
    return None


def resolve_image_file(payload: Mapping[str, Any], work_dir: Path) -> Path | None:
    manifest_image = image_from_resource_manifest(payload)
    if manifest_image is not None:
        return manifest_image

    direct = payload.get("image_file")
    if isinstance(direct, Mapping):
        path = write_artifact(direct, work_dir)
        if path is not None:
            return path

    artifacts = payload.get("uploaded_artifacts")
    if isinstance(artifacts, list | tuple):
        for item in artifacts:
            if not isinstance(item, Mapping):
                continue
            path = write_artifact(item, work_dir)
            if path is not None and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                return path
    return None


def multipart_body(field_name: str, file_path: Path) -> tuple[bytes, str]:
    boundary = "----plantdis-" + uuid.uuid4().hex
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{file_path.name}"\r\n'
        ).encode("utf-8"),
        f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"),
        file_path.read_bytes(),
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts), boundary


def post_image(endpoint: str, image_path: Path, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    query = ""
    if params:
        encoded = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if encoded:
            query = f"?{encoded}"
    url = f"{imgpu_base_url()}{endpoint}{query}"
    body, boundary = multipart_body("file", image_path)
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def confidence_value(payload: Mapping[str, Any], key: str, default: float) -> float:
    try:
        value = float(payload.get(key, default))
    except (TypeError, ValueError):
        value = default
    return min(1.0, max(0.0, value))


def normalize_result(crop: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return {
            "crop": crop,
            "model_name": payload.get("model_name"),
            "detected": False,
            "disease_name_zh": None,
            "disease_label": None,
            "confidence": None,
        }
    return {
        "crop": crop,
        "model_name": payload.get("model_name"),
        "detected": True,
        "disease_name_zh": result.get("chinese_name"),
        "disease_label": result.get("english_label"),
        "confidence": result.get("confidence"),
    }


def format_confidence(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "未知"


def answer_for(result: Mapping[str, Any]) -> str:
    crop_label = CROP_LABELS.get(str(result.get("crop")), str(result.get("crop") or "未知作物"))
    if not result.get("detected"):
        return (
            f"识别结果：{crop_label}，本次图片未检测到当前支持的病害目标。"
            "建议重新上传或拍摄更清晰、主体更完整的叶片图片。模型结果仅作辅助判断。"
        )

    disease_name = str(result.get("disease_name_zh") or "未知病害")
    disease_label = str(result.get("disease_label") or "unknown")
    confidence = format_confidence(result.get("confidence"))
    return (
        f"识别结果：{crop_label}，模型识别为{disease_name}（{disease_label}），"
        f"置信度 {confidence}。模型结果仅作辅助判断，请结合田间症状和专业判断复核。"
    )


def run(payload: Mapping[str, Any]) -> dict[str, Any]:
    crop = normalize_crop(payload.get("crop"))
    if crop is None:
        return fail("这张图片是玉米还是水稻？当前不支持从图片自动识别作物类型。", missing=["crop"], error_type="missing_crop")

    with tempfile.TemporaryDirectory(prefix="plant-dis-") as tmp:
        image_path = resolve_image_file(payload, Path(tmp))
        if image_path is None:
            return fail(
                "请在当前会话上传一张玉米或水稻叶片图片，或使用相机拍照后提交。",
                missing=["image_file"],
                error_type="missing_image",
            )
        if image_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return fail("请上传 jpg、jpeg、png、webp 或 bmp 格式的叶片图片。", error_type="unsupported_image_format")

        try:
            if crop == "rice":
                data = post_image(
                    "/v1/rice/detect",
                    image_path,
                    params={
                        "confidence": confidence_value(payload, "confidence", 0.25),
                        "iou": confidence_value(payload, "iou", 0.45),
                    },
                )
            else:
                data = post_image("/v1/maize/classify", image_path)
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                return fail("图片无法解析，请上传清晰、常见格式的叶片图片。", error_type="invalid_image")
            if exc.code == 413:
                return fail("图片过大，请压缩图片后重试。", error_type="image_too_large")
            return fail("病害识别服务暂时返回错误，请稍后重试。", error_type="imgpu_http_error")
        except TimeoutError:
            return fail("病害识别服务请求超时，请稍后重试。", error_type="imgpu_timeout")
        except Exception:
            return fail("病害识别服务暂时不可用，请稍后重试。", error_type="imgpu_unavailable")

    result = normalize_result(crop, data)
    answer = answer_for(result)
    return {
        "ok": True,
        "answer": answer,
        "response_text": answer,
        "result": result,
    }


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        emit(fail("脚本输入必须是 JSON object。", error_type="invalid_stdin"))
        return
    if not isinstance(payload, dict):
        emit(fail("脚本输入必须是 JSON object。", error_type="invalid_stdin"))
        return
    emit(run(payload))


if __name__ == "__main__":
    main()

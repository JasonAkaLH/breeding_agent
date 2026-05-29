from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
QTN_CHECK = SCRIPT_DIR / "rice_qtn_check.py"
INTERPRET = SCRIPT_DIR / "interpret_gene_check.py"


def emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))


def fail(answer: str, *, missing: list[str] | None = None, error_type: str = "rice_genie_error") -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "is_error": True,
        "answer": answer,
        "error": {"type": error_type, "message": answer},
    }
    if missing:
        result["missing"] = missing
    return result


def safe_token(value: Any, default: str = "rice_genie") -> str:
    text = str(value or "").strip() or default
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")
    return text[:80] or default


def artifact_filename(artifact: Mapping[str, Any], default: str = "sample.vcf") -> str:
    raw = str(artifact.get("filename") or default)
    name = Path(raw).name.replace("\x00", "")
    if not name or name in {".", ".."}:
        return default
    lower = name.lower()
    if lower.endswith((".vcf", ".vcf.gz", ".gene_check.json", ".json")):
        return name
    return f"{Path(name).stem or 'sample'}.vcf"


def decode_artifact_content(artifact: Mapping[str, Any]) -> bytes | None:
    if isinstance(artifact.get("content"), str):
        return str(artifact["content"]).encode("utf-8")
    if isinstance(artifact.get("content_base64"), str):
        try:
            return base64.b64decode(str(artifact["content_base64"]), validate=True)
        except Exception:
            return None
    return None


def write_uploaded_input(payload: Mapping[str, Any], work_dir: Path) -> Path | None:
    artifacts = payload.get("uploaded_artifacts")
    if not isinstance(artifacts, list | tuple):
        return None
    for item in artifacts:
        if not isinstance(item, Mapping):
            continue
        content = decode_artifact_content(item)
        if content is None:
            continue
        path = work_dir / artifact_filename(item)
        path.write_bytes(content)
        return path
    return None


def resolve_input_path(payload: Mapping[str, Any], work_dir: Path) -> Path | None:
    uploaded = write_uploaded_input(payload, work_dir)
    if uploaded is not None:
        return uploaded
    for key in ("input_file", "file_path", "path", "vcf"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            candidate = Path(raw).expanduser()
            if candidate.exists() and candidate.is_file():
                return candidate.resolve()
    return None


def is_gene_check_json(path: Path) -> bool:
    if path.name.endswith(".gene_check.json"):
        return True
    if path.suffix.lower() != ".json":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("samples"), dict) and isinstance(data.get("metadata"), dict)


def output_file(path: Path, *, label: str, summary: str) -> dict[str, str]:
    return {
        "path": f"outputs/{path.name}",
        "filename": path.name,
        "mime_type": "text/markdown",
        "label": label,
        "summary": summary,
    }


def run_process(command: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)


def process_error(process: subprocess.CompletedProcess[str]) -> str:
    return (process.stderr or process.stdout or f"process exited with {process.returncode}")[-1200:].strip()


def run_interpret(gene_check_path: Path, report_path: Path, payload: Mapping[str, Any]) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(INTERPRET), "--input", str(gene_check_path), "--mode", "key-trait-report", "--output", str(report_path)]
    sample = payload.get("sample")
    samples = payload.get("samples")
    if isinstance(sample, str) and sample.strip():
        command.extend(["--sample", sample.strip()])
    elif isinstance(samples, str) and samples.strip():
        command.extend(["--samples", samples.strip()])
    return run_process(command)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        emit(fail("脚本输入必须是 JSON object。", error_type="invalid_stdin"))
        return 0
    if not isinstance(payload, dict):
        emit(fail("脚本输入必须是 JSON object。", error_type="invalid_stdin"))
        return 0

    output_dir = Path(os.environ.get("MAF_SKILL_OUTPUT_DIR") or "outputs").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = safe_token(payload.get("run_id") or payload.get("out_prefix") or payload.get("out-prefix") or "rice_genie")

    with tempfile.TemporaryDirectory(prefix="rice-genie-input-") as tmp:
        input_path = resolve_input_path(payload, Path(tmp))
        if input_path is None:
            emit(fail("请上传水稻 VCF/VCF.GZ 文件，或上传已有 gene_check JSON 结果。", missing=["rice_input"], error_type="missing_input"))
            return 0

        if is_gene_check_json(input_path):
            gene_check_path = input_path
        else:
            lower = input_path.name.lower()
            if not lower.endswith((".vcf", ".vcf.gz")):
                emit(fail("输入文件必须是 .vcf、.vcf.gz 或 .gene_check.json。", error_type="invalid_input"))
                return 0
            check_command = [sys.executable, str(QTN_CHECK), "--vcf", str(input_path), "--outdir", str(output_dir), "--out-prefix", prefix]
            sample = payload.get("sample")
            samples = payload.get("samples")
            if isinstance(sample, str) and sample.strip():
                check_command.extend(["--sample", sample.strip()])
            elif isinstance(samples, str) and samples.strip():
                for item in re.split(r"[,，;；\s]+", samples.strip()):
                    if item:
                        check_command.extend(["--sample", item])
            process = run_process(check_command)
            if process.returncode != 0:
                emit(fail("水稻 QTN 匹配执行失败：" + process_error(process), error_type="qtn_check_failed"))
                return 0
            gene_check_path = output_dir / f"{prefix}.gene_check.json"
            if not gene_check_path.exists():
                emit(fail("水稻 QTN 匹配完成但未生成预期结果。", error_type="result_missing"))
                return 0

        report_path = output_dir / f"rice-genie-{prefix}-report.md"
        interpret = run_interpret(gene_check_path, report_path, payload)
        if interpret.returncode != 0:
            emit(fail("水稻基因型体检报告生成失败：" + process_error(interpret), error_type="interpret_failed"))
            return 0
        answer = report_path.read_text(encoding="utf-8-sig") if report_path.exists() else interpret.stdout
        answer = answer.strip()
        if not answer:
            emit(fail("水稻基因型体检报告为空。", error_type="empty_report"))
            return 0

    emit(
        {
            "ok": True,
            "answer": answer,
            "report_format": "rice-genie-key-trait-report-v1",
            "output_files": [
                output_file(report_path, label="水稻基因型体检报告 Markdown", summary="基于当前 QTN 匹配事实生成的用户报告。")
            ],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

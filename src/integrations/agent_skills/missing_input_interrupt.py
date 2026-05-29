from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from src.core.contracts import CapabilityExecutionRequest
from src.core.enums import InterruptStatus
from src.core.models import Interrupt

from .manifest import SkillManifest
from .parameters import SkillParameterSpec


_FIELD_LABELS = {
    "blocks": "区组数/重复数",
    "ck_spec": "CK 起始位置和间隔",
    "design": "设计类型",
    "field_data": "田间表型数据文件",
    "file_path": "图片/PDF 文件或本地文件路径",
    "material_data": "试验材料 CSV/JSON 文件",
    "ncols": "田块列数",
    "query": "用户问题",
    "rice_input": "水稻 VCF/VCF.GZ 或 gene_check JSON 文件",
    "sample": "样本名",
    "samples": "样本列表",
    "variety": "品种名称",
}

_FIELD_DESCRIPTIONS = {
    "blocks": "随机区组重复数，例如 3。",
    "ck_spec": "Interval 间比法 CK 参数，格式：ck_no,start_pos,interval；多个 CK 用分号分隔。",
    "design": "设计类型，例如 rcbd、diagonal 或 interval。",
    "field_data": "请上传包含田间表型数据的 CSV/JSON 文件。",
    "file_path": "请上传图片/PDF，或提供可访问的本地文件路径。",
    "material_data": "请上传试验材料 CSV/JSON 文件；推荐列名 ped_id,hyb_check,set。",
    "ncols": "田块列数，例如 10。",
    "query": "请补充要处理的问题或指令。",
    "rice_input": "请上传水稻 VCF/VCF.GZ 文件，或已有 gene_check JSON 结果。",
    "sample": "请补充样本名。",
    "samples": "请补充样本列表。",
    "variety": "请补充品种名称。",
}

_ARTIFACT_FIELDS = {"field_data", "file_path", "material_data", "rice_input"}


def missing_input_fields_from_payload(output_payload: Mapping[str, Any]) -> tuple[str, ...]:
    error = output_payload.get("error") if isinstance(output_payload.get("error"), Mapping) else {}
    error_type = str(error.get("type") or output_payload.get("error_type") or "").strip().lower()
    if error_type != "missing_input":
        return ()
    return _clean_missing_fields(output_payload.get("missing"))


def build_missing_input_interrupt(
    *,
    request: CapabilityExecutionRequest,
    manifest: SkillManifest,
    skill_name: str,
    entrypoint: str,
    missing: Iterable[str],
) -> Interrupt | None:
    missing_fields = _clean_missing_fields(missing)
    if not missing_fields:
        return None
    digest = hashlib.sha256(
        f"{request.node_id}:{skill_name}:{entrypoint}:{','.join(missing_fields)}".encode("utf-8")
    ).hexdigest()[:12]
    return Interrupt(
        interrupt_id=f"{request.node_id}:interrupt:skill_input_missing:{digest}",
        conversation_id=request.conversation_id,
        task_id=request.task_id,
        node_id=request.node_id,
        source_agent=f"skill.{skill_name}",
        source_message_id=str(request.input_payload.get("message_id") or request.task_id),
        question=_missing_input_question(manifest, missing_fields),
        reason_code=_missing_reason_code(missing_fields),
        required_fields={
            name: _required_field_payload(name, manifest.parameters.get(name))
            for name in missing_fields
        },
        status=InterruptStatus.OPEN,
    )


def _clean_missing_fields(raw_missing: Any) -> tuple[str, ...]:
    if raw_missing is None:
        values: Iterable[Any] = ()
    elif isinstance(raw_missing, str):
        values = (raw_missing,)
    elif isinstance(raw_missing, Iterable):
        values = raw_missing
    else:
        values = (raw_missing,)
    return tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _missing_input_question(manifest: SkillManifest, missing_fields: tuple[str, ...]) -> str:
    skill_label = str(manifest.metadata.get("display_name") or manifest.name).strip() or manifest.name
    labels = "、".join(_FIELD_LABELS.get(field, field) for field in missing_fields)
    if any(field in _ARTIFACT_FIELDS for field in missing_fields):
        return f"{skill_label} 还缺少：{labels}。请上传对应文件，或按提示补充可解析的信息后继续。"
    return f"{skill_label} 还缺少：{labels}。请在输入框补充后继续当前任务。"


def _missing_reason_code(missing_fields: tuple[str, ...]) -> str:
    if len(missing_fields) == 1:
        return f"missing_{missing_fields[0]}"
    return "missing_skill_input"


def _required_field_payload(name: str, spec: SkillParameterSpec | None) -> dict[str, Any]:
    field_type = spec.type if spec is not None else "string"
    payload: dict[str, Any] = {
        "type": field_type,
        "description": _FIELD_DESCRIPTIONS.get(name, f"请补充 {name}。"),
    }
    if spec is not None and spec.aliases:
        payload["aliases"] = list(spec.aliases)
    if name in _ARTIFACT_FIELDS or field_type in {"artifact", "file", "data"}:
        payload["accepts_upload"] = True
    return payload

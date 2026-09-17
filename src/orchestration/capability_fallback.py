from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

CAPABILITY_MISSING_FALLBACK_KEY = "capability_missing_fallback"
CAPABILITY_MISSING_FALLBACK_EVENT = "capability.missing_fallback"

_ALLOWED_SCOPES = {"full", "partial"}
_ALLOWED_REASON_CODES = {
    "capability_missing",
    "skill_missing",
    "forced_skill_missing",
    "mcp_missing",
}
_REASON_PRIORITY = {
    "forced_skill_missing": 0,
    "skill_missing": 1,
    "mcp_missing": 2,
    "capability_missing": 3,
}
_TEXT_LIMIT = 240
_SOURCE_MESSAGE_ID_LIMIT = 20
_FORBIDDEN_GENERATED_ARTIFACT_PATTERNS = (
    re.compile(r"(文件|报告|表格|图|材料|结果|artifact|file).{0,8}(已|已经|成功|正在).{0,8}(生成|创建|导出|产出|保存)", re.IGNORECASE),
    re.compile(r"(已|已经|成功|正在).{0,8}(生成|创建|导出|产出|保存).{0,8}(文件|报告|表格|图|材料|结果|artifact|file)", re.IGNORECASE),
    re.compile(r"(点击|请点击|可点击|可以点击).{0,8}(下载|获取)", re.IGNORECASE),
    re.compile(r"(下载|获取).{0,6}(链接|地址|url|URL|路径|path)", re.IGNORECASE),
    re.compile(r"(链接|地址|url|URL|路径|path).{0,6}(下载|获取)", re.IGNORECASE),
    re.compile(r"(见|查看|打开).{0,4}(附件|附档|文件|下载)", re.IGNORECASE),
    re.compile(r"(已|已经|成功).{0,6}(完成|办好|做好).{0,8}(文件|报告|表格|图|田间图|材料|结果|artifact|file)", re.IGNORECASE),
    re.compile(r"(文件|报告|表格|图|田间图|材料|结果|artifact|file).{0,8}(已|已经|成功).{0,6}(完成|办好|做好)", re.IGNORECASE),
    re.compile(r"(后台|系统).{0,8}(正在|已|已经).{0,8}(生成|导出|处理)", re.IGNORECASE),
    re.compile(r"(已|已经|成功|正在).{0,8}(调用|执行).{0,8}(Skill|MCP|工具|能力)", re.IGNORECASE),
)
_FALLBACK_NEGATION_MARKERS = ("不会", "不能", "不得", "未", "没有", "不会声称", "不会生成", "未生成", "未调用")
_FALLBACK_ASSERTION_REPLACEMENT = "我不会声称已有文件产物、提供下载或调用缺失能力；以下仅保留可手工复核的文字建议。"

MetadataMode = Literal["runtime", "history"]


def sanitize_capability_missing_fallback_metadata(
    value: Any,
    *,
    mode: MetadataMode = "history",
    has_executed_business_result: bool | None = None,
) -> dict[str, Any] | None:
    """Return user-safe capability-missing fallback metadata or None.

    The sanitizer is deliberately closed: only the PRD allowlist survives.  The
    runtime mode currently returns the same public-safe shape plus optional
    public capability summaries; prompt/runtime-only fields such as raw prompts,
    file contents, handler paths, storage refs, and internal module names are
    never copied.
    """

    if not isinstance(value, Mapping):
        return None
    if value.get("enabled") is not True:
        return None

    scope = _safe_choice(value.get("scope"), _ALLOWED_SCOPES, default="full")
    if scope == "partial" and has_executed_business_result is False:
        scope = "full"

    reason_code = _safe_choice(value.get("reason_code"), _ALLOWED_REASON_CODES, default="capability_missing")
    missing_summary = _safe_text(
        value.get("missing_capability_summary")
        or value.get("missing_skill_summary")
        or value.get("missing_summary")
        or _default_missing_summary(reason_code)
    )
    attempted_summary = _safe_text(value.get("attempted_capability_summary"))
    if scope == "partial" and not attempted_summary:
        attempted_summary = "已先执行可用能力结果；以下仅补充未覆盖的能力缺口。"
    fallback_content_scope = _safe_text(
        value.get("fallback_content_scope")
        or "仅覆盖未找到可执行业务能力的说明、草案或可手工复核建议。"
    )

    sanitized: dict[str, Any] = {
        "enabled": True,
        "scope": scope,
        "reason_code": reason_code,
        "missing_capability_summary": missing_summary,
        "fallback_content_scope": fallback_content_scope,
        "llm_fallback_allowed": _safe_bool(value.get("llm_fallback_allowed"), default=True),
        "artifact_generation_allowed": False,
        "disclosure_required": True,
        "memory_context_used": _safe_bool(value.get("memory_context_used"), default=False),
        "source_message_count": _safe_non_negative_int(value.get("source_message_count"), default=0),
    }
    if attempted_summary:
        sanitized["attempted_capability_summary"] = attempted_summary

    source_message_ids = _safe_source_message_ids(value.get("source_message_ids"))
    if source_message_ids:
        sanitized["source_message_ids"] = source_message_ids

    if mode == "runtime":
        available = _safe_capability_summaries(value.get("available_capabilities"))
        if available:
            sanitized["available_capabilities"] = available
    return sanitized


def fallback_metadata_from_container(
    container: Mapping[str, Any] | None,
    *,
    mode: MetadataMode = "history",
    has_executed_business_result: bool | None = None,
) -> dict[str, Any] | None:
    if not isinstance(container, Mapping):
        return None
    value = container.get(CAPABILITY_MISSING_FALLBACK_KEY)
    return sanitize_capability_missing_fallback_metadata(
        value,
        mode=mode,
        has_executed_business_result=has_executed_business_result,
    )


def merge_capability_missing_fallback_metadata(
    values: Sequence[Mapping[str, Any] | None],
    *,
    mode: MetadataMode = "history",
    has_executed_business_result: bool | None = None,
) -> dict[str, Any] | None:
    sanitized_values = [
        sanitized
        for value in values
        if (
            sanitized := sanitize_capability_missing_fallback_metadata(
                value,
                mode=mode,
                has_executed_business_result=has_executed_business_result,
            )
        )
        is not None
    ]
    if not sanitized_values:
        return None
    chosen = min(
        sanitized_values,
        key=lambda item: _REASON_PRIORITY.get(str(item.get("reason_code") or "capability_missing"), 99),
    )
    scope = "partial" if any(item.get("scope") == "partial" for item in sanitized_values) else "full"
    if has_executed_business_result is False:
        scope = "full"
    attempted = next(
        (str(item.get("attempted_capability_summary") or "").strip() for item in sanitized_values if item.get("attempted_capability_summary")),
        "",
    )
    merged = dict(chosen)
    merged["scope"] = scope
    merged["llm_fallback_allowed"] = any(bool(item.get("llm_fallback_allowed")) for item in sanitized_values)
    merged["artifact_generation_allowed"] = False
    merged["disclosure_required"] = True
    merged["memory_context_used"] = any(bool(item.get("memory_context_used")) for item in sanitized_values)
    merged["source_message_count"] = max(_safe_non_negative_int(item.get("source_message_count"), default=0) for item in sanitized_values)
    if scope == "partial" and attempted:
        merged["attempted_capability_summary"] = attempted
    elif scope == "full":
        merged.pop("attempted_capability_summary", None)
    return sanitize_capability_missing_fallback_metadata(
        merged,
        mode=mode,
        has_executed_business_result=has_executed_business_result,
    )


def build_capability_missing_fallback_metadata(
    *,
    reason_code: str = "capability_missing",
    scope: str = "full",
    missing_capability_summary: str | None = None,
    attempted_capability_summary: str | None = None,
    fallback_content_scope: str | None = None,
    memory_context_used: bool = False,
    source_message_count: int = 0,
    source_message_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "enabled": True,
        "scope": scope,
        "reason_code": reason_code,
        "missing_capability_summary": missing_capability_summary or _default_missing_summary(reason_code),
        "fallback_content_scope": fallback_content_scope or "仅覆盖未找到可执行业务能力的说明、草案或可手工复核建议。",
        "llm_fallback_allowed": True,
        "artifact_generation_allowed": False,
        "disclosure_required": True,
        "memory_context_used": memory_context_used,
        "source_message_count": source_message_count,
    }
    if attempted_capability_summary:
        raw["attempted_capability_summary"] = attempted_capability_summary
    if source_message_ids:
        raw["source_message_ids"] = tuple(source_message_ids)
    return sanitize_capability_missing_fallback_metadata(raw, mode="history") or {
        "enabled": True,
        "scope": "full",
        "reason_code": "capability_missing",
        "missing_capability_summary": _default_missing_summary("capability_missing"),
        "fallback_content_scope": "仅覆盖未找到可执行业务能力的说明、草案或可手工复核建议。",
        "llm_fallback_allowed": True,
        "artifact_generation_allowed": False,
        "disclosure_required": True,
        "memory_context_used": False,
        "source_message_count": 0,
    }


def fallback_disclosure_prefix(metadata: Mapping[str, Any]) -> str:
    sanitized = sanitize_capability_missing_fallback_metadata(metadata, mode="history")
    if sanitized is None:
        return ""
    scope = str(sanitized.get("scope") or "full")
    missing = str(sanitized.get("missing_capability_summary") or _default_missing_summary(str(sanitized.get("reason_code") or "capability_missing")))
    fallback_scope = str(sanitized.get("fallback_content_scope") or "仅提供通用 LLM 回答。")
    if scope == "partial":
        attempted = str(sanitized.get("attempted_capability_summary") or "已先执行可用能力结果")
        return f"【能力缺口说明】{attempted}；但{missing}。以下内容由通用 LLM 补充，{fallback_scope}，未调用缺失能力，也不会生成可下载文件。"
    return f"【能力缺口说明】{missing}。以下内容由通用 LLM 回答，{fallback_scope}；本次未调用对应业务能力，也不会生成可下载文件。"


def ensure_fallback_disclosure(text: str, metadata: Mapping[str, Any] | None) -> str:
    if not metadata:
        return text
    prefix = fallback_disclosure_prefix(metadata)
    if not prefix:
        return text
    safe_text = _remove_forbidden_fallback_assertions(text)
    stripped = safe_text.lstrip()
    if stripped.startswith("【能力缺口说明】"):
        return safe_text
    return f"{prefix}\n\n{safe_text}" if safe_text else prefix


def _remove_forbidden_fallback_assertions(text: str) -> str:
    if not text:
        return text
    chunks = re.split(r"([。！？!?；;\n]+)", text)
    kept: list[str] = []
    removed = False
    for index in range(0, len(chunks), 2):
        sentence = chunks[index]
        separator = chunks[index + 1] if index + 1 < len(chunks) else ""
        if not sentence:
            if separator:
                kept.append(separator)
            continue
        if _is_forbidden_fallback_assertion(sentence):
            removed = True
            continue
        kept.append(sentence + separator)
    cleaned = "".join(kept).strip()
    if not removed:
        return text
    return f"{_FALLBACK_ASSERTION_REPLACEMENT}\n\n{cleaned}" if cleaned else _FALLBACK_ASSERTION_REPLACEMENT


def _is_forbidden_fallback_assertion(sentence: str) -> bool:
    normalized = " ".join(sentence.split())
    if not normalized:
        return False
    if any(marker in normalized for marker in _FALLBACK_NEGATION_MARKERS):
        return False
    return any(pattern.search(normalized) for pattern in _FORBIDDEN_GENERATED_ARTIFACT_PATTERNS)


def _safe_choice(value: Any, allowed: set[str], *, default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default


def _safe_text(value: Any, *, limit: int = _TEXT_LIMIT) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _safe_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return default


def _safe_non_negative_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _safe_source_message_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    ids: list[str] = []
    for item in value:
        text = _safe_text(item, limit=80)
        if not text or text in ids:
            continue
        ids.append(text)
        if len(ids) >= _SOURCE_MESSAGE_ID_LIMIT:
            break
    return tuple(ids)


def _safe_capability_summaries(value: Any) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list | tuple):
        return ()
    summaries: list[dict[str, str]] = []
    for item in value[:20]:
        if not isinstance(item, Mapping):
            continue
        capability_id = _safe_text(item.get("capability_id") or item.get("id"), limit=120)
        name = _safe_text(item.get("name") or item.get("display_name"), limit=120)
        description = _safe_text(item.get("description"), limit=180)
        if capability_id:
            summaries.append({"capability_id": capability_id, "name": name, "description": description})
    return tuple(summaries)


def _default_missing_summary(reason_code: str) -> str:
    if reason_code == "forced_skill_missing":
        return "用户点名的 Skill 未注册或当前不可用"
    if reason_code == "skill_missing":
        return "当前 Skill 能力库中没有匹配的可执行能力"
    if reason_code == "mcp_missing":
        return "当前 MCP 工具库中没有匹配的可执行能力"
    return "当前公开能力库中没有匹配的业务能力"

from __future__ import annotations

import json
from typing import Any, Mapping

from src.integrations.codex_skills import SkillMatch

_SENSITIVE_ARTIFACT_KEYS = {"content", "raw", "text", "storage_ref", "path", "file_path", "local_path"}


def build_main_agent_prompt(
    *,
    user_message: str,
    skill_matches: list[SkillMatch],
    artifact_context: list[dict[str, Any]],
    script_results: list[dict[str, Any]],
    dependency_context: list[dict[str, Any]] | None = None,
) -> str:
    parts = [
        "你是小奥 Agent 的主代理。",
        "你需要直接回答用户问题；如果注入了 Skill 指令，优先遵循 Skill 的工作流和输出要求。",
        "你必须用第一性原理理解用户需求：不要假定用户每次都知道自己要什么、该选哪个 capability 或该提供哪些参数；先从用户真实目标、上下文和可用能力出发推断最有帮助的下一步。",
        "遇到宽泛问题时，优先给出可验证的初步答案、合理假设和下一步建议；只有在缺少关键事实会导致误导或无法安全执行时，才提出一个最关键的澄清问题。",
        "不要编造未提供的文件内容；上传文件只可信任下方 artifact 摘要和 metadata。",
    ]
    if artifact_context:
        parts.append("\n# 上传文件上下文（已脱敏）\n" + json.dumps(artifact_context, ensure_ascii=False, indent=2, default=str))
    if dependency_context:
        parts.append(
            "\n# 上游能力结果上下文（已执行完成）\n"
            "这些内容来自自动 DAG 中已经完成的能力节点。请优先基于这些事实回答用户，并把技术性字段整理成自然语言。\n"
            + json.dumps(dependency_context, ensure_ascii=False, indent=2, default=str)
        )
    if skill_matches:
        skill_blocks = []
        for match in skill_matches:
            skill_blocks.append(
                f"## Skill: {match.manifest.name}\n"
                f"Description: {match.manifest.description}\n"
                f"Match reason: {match.reason}\n\n"
                f"{match.manifest.body}"
            )
        parts.append("\n# 已匹配 Skill 指令\n" + "\n\n".join(skill_blocks))
    if script_results:
        parts.append("\n# Skill 脚本输出\n" + json.dumps(script_results, ensure_ascii=False, indent=2, default=str))
    parts.append("\n# 用户问题\n" + user_message)
    return "\n".join(parts)


def build_dependency_context(dependency_outputs: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    context: list[dict[str, Any]] = []
    for node_id, output in dependency_outputs.items():
        if not isinstance(output, Mapping):
            continue
        safe_output = _sanitize_dependency_output(output)
        if safe_output:
            context.append({"node_id": node_id, **safe_output})
    return context


def build_artifact_context(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_items = metadata.get("uploaded_artifacts") or metadata.get("artifacts") or ()
    if not isinstance(raw_items, list | tuple):
        return []
    sanitized: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        safe = {
            str(key): value
            for key, value in item.items()
            if str(key).lower() not in _SENSITIVE_ARTIFACT_KEYS
        }
        if safe:
            sanitized.append(safe)
    return sanitized


def _sanitize_dependency_output(output: Mapping[str, Any]) -> dict[str, Any]:
    allowlist = (
        "summary",
        "response_text",
        "route_id",
        "schema_profile_id",
        "columns",
        "rows",
        "row_count",
        "preview_row_count",
        "truncated",
        "summary_source",
        "fallback_used",
        "fallback_reason",
        "source_row_count",
        "source_preview_row_count",
        "candidate_row_count",
        "removed_row_count",
        "kept_row_indexes",
        "filter_source",
        "filter_reason",
        "highlights",
        "caveats",
    )
    return {key: output[key] for key in allowlist if key in output}

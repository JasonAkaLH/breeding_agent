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
) -> str:
    parts = [
        "你是小奥 Agent 的主代理。",
        "你需要直接回答用户问题；如果注入了 Skill 指令，优先遵循 Skill 的工作流和输出要求。",
        "不要编造未提供的文件内容；上传文件只可信任下方 artifact 摘要和 metadata。",
    ]
    if artifact_context:
        parts.append("\n# 上传文件上下文（已脱敏）\n" + json.dumps(artifact_context, ensure_ascii=False, indent=2, default=str))
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

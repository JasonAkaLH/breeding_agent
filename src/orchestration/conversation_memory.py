from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from src.core.enums import ArtifactType, MessageRole, TaskStatus
from src.core.models import Artifact, ConversationMemorySummary, Message, Task
from src.integrations.llm_client import load_config
from src.integrations.token_counter import get_num_of_tokens_from_messages

from .models import OrchestrationRequest

SUMMARY_VERSION = "conversation-memory-summary-v1"
COMPRESSION_POLICY_VERSION = "conversation-memory-policy-v1"

SummaryGenerator = Callable[[str], str | Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ConversationMemoryConfig:
    max_tokens: int | None = None
    recent_turns: int = 6
    summary_max_tokens: int | None = None
    enable_summary_llm: bool = True
    reserved_tokens: int | None = None

    @classmethod
    def from_runtime_config(cls, config: Mapping[str, Any] | None = None) -> "ConversationMemoryConfig":
        loaded = dict(config) if config is not None else load_config()
        max_tokens = _coerce_positive_int(loaded.get("trim_max_tokens"))
        recent_turns = _coerce_positive_int(loaded.get("conversation_memory_recent_turns")) or 6
        summary_max_tokens = _coerce_positive_int(loaded.get("conversation_memory_summary_max_tokens"))
        enable_summary_llm = _coerce_bool(loaded.get("conversation_memory_enable_summary_llm"), default=True)
        return cls(
            max_tokens=max_tokens,
            recent_turns=recent_turns,
            summary_max_tokens=summary_max_tokens,
            enable_summary_llm=enable_summary_llm,
        )

    @property
    def actual_memory_budget(self) -> int:
        max_tokens = self.max_tokens if self.max_tokens is not None and self.max_tokens > 0 else 8_000
        reserved = self.reserved_tokens if self.reserved_tokens is not None else max(1_024, max_tokens // 4)
        return max(512, max_tokens - reserved)

    @property
    def effective_summary_max_tokens(self) -> int:
        budget = self.actual_memory_budget
        configured = self.summary_max_tokens
        if configured is None:
            configured = min(4_096, max(1_024, budget // 4))
        return max(1, min(configured, budget))


@dataclass(frozen=True, slots=True)
class ConversationMemoryMessage:
    message_id: str
    role: str
    content: str
    task_id: str | None = None
    created_at: datetime | None = None
    kind: str = "message"

    @classmethod
    def from_message(cls, message: Message, *, kind: str = "message") -> "ConversationMemoryMessage":
        return cls(
            message_id=message.message_id,
            role=str(message.role),
            content=message.content,
            task_id=message.task_id,
            created_at=message.created_at,
            kind=kind,
        )

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "content": self.content,
            "task_id": self.task_id,
            "kind": self.kind,
            "created_at": self.created_at.isoformat() if self.created_at is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ConversationMemoryContext:
    conversation_id: str
    root_message_id: str
    source_message_count: int
    current_user_message: str
    resolved_user_message: str | None = None
    recent_messages: tuple[ConversationMemoryMessage, ...] = ()
    clarification_messages: tuple[ConversationMemoryMessage, ...] = ()
    history_summary: str | None = None
    capability_summaries: tuple[dict[str, Any], ...] = ()
    compression_level: str = "none"
    token_budget: int = 0
    estimated_tokens_before: int = 0
    estimated_tokens_after: int = 0
    truncated: bool = False
    fallback_reason: str | None = None
    resolution_metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def effective_user_message(self) -> str:
        resolved = (self.resolved_user_message or "").strip()
        return resolved or self.current_user_message

    def to_prompt_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "current_user_message": self.current_user_message,
            "resolved_user_message": self.resolved_user_message,
            "history_summary": self.history_summary,
            "recent_messages": [message.to_prompt_dict() for message in self.recent_messages],
            "clarification_messages": [message.to_prompt_dict() for message in self.clarification_messages],
            "capability_summaries": [dict(item) for item in self.capability_summaries],
            "compression_level": self.compression_level,
            "token_budget": self.token_budget,
            "estimated_tokens_before": self.estimated_tokens_before,
            "estimated_tokens_after": self.estimated_tokens_after,
            "truncated": self.truncated,
            "fallback_reason": self.fallback_reason,
            "resolution_metadata": dict(self.resolution_metadata),
        }
        return _strip_none(payload)

    def to_audit_payload(self) -> dict[str, Any]:
        return {
            "source_message_count": self.source_message_count,
            "recent_message_count": len(self.recent_messages),
            "clarification_message_count": len(self.clarification_messages),
            "capability_summary_count": len(self.capability_summaries),
            "compression_level": self.compression_level,
            "token_budget": self.token_budget,
            "estimated_tokens_before": self.estimated_tokens_before,
            "estimated_tokens_after": self.estimated_tokens_after,
            "truncated": self.truncated,
            "fallback_reason": self.fallback_reason,
            "resolved": bool(self.resolved_user_message),
        }


class ConversationMemorySafeAllowlist:
    _ALLOWED_OUTPUT_KEYS = {
        "summary",
        "response_text",
        "route_id",
        "schema_profile_id",
        "row_count",
        "preview_row_count",
        "source_row_count",
        "candidate_row_count",
        "removed_row_count",
        "filter_source",
        "filter_reason",
        "highlights",
        "caveats",
        "truncated",
    }
    _UPLOAD_KEYS = {
        "upload_id",
        "filename",
        "content_type",
        "file_type",
        "size_bytes",
        "sha256",
        "preview",
        "expires_at",
    }

    @classmethod
    def project_capability_output(cls, output: Mapping[str, Any], *, max_columns: int = 12) -> dict[str, Any]:
        safe = {key: output[key] for key in cls._ALLOWED_OUTPUT_KEYS if key in output}
        if "columns" in output and isinstance(output["columns"], list | tuple):
            columns = [str(column) for column in output["columns"][:max_columns]]
            safe["columns"] = columns
            if len(output["columns"]) > max_columns:
                safe["truncated"] = True
        return _json_safe_mapping(safe)

    @classmethod
    def project_upload_summary(cls, upload: Mapping[str, Any]) -> dict[str, Any]:
        return _json_safe_mapping({key: upload[key] for key in cls._UPLOAD_KEYS if key in upload})


@dataclass(slots=True)
class _BusinessTurn:
    turn_id: str
    root: Message | None = None
    clarifications: list[Message] = field(default_factory=list)
    assistant: Message | None = None
    artifact_fallback: Artifact | None = None

    @property
    def created_at(self) -> datetime | None:
        for item in (self.root, self.assistant):
            if item is not None and item.created_at is not None:
                return item.created_at
        if self.clarifications and self.clarifications[0].created_at is not None:
            return self.clarifications[0].created_at
        return None

    def memory_messages(self) -> list[ConversationMemoryMessage]:
        messages: list[ConversationMemoryMessage] = []
        if self.root is not None:
            messages.append(ConversationMemoryMessage.from_message(self.root, kind="root"))
        messages.extend(ConversationMemoryMessage.from_message(message, kind="clarification") for message in self.clarifications)
        if self.assistant is not None:
            messages.append(ConversationMemoryMessage.from_message(self.assistant, kind="assistant"))
        elif self.artifact_fallback is not None and self.artifact_fallback.storage_ref.strip():
            messages.append(
                ConversationMemoryMessage(
                    message_id=f"{self.artifact_fallback.task_id}:assistant_artifact",
                    role=str(MessageRole.ASSISTANT),
                    content=self.artifact_fallback.storage_ref,
                    task_id=self.artifact_fallback.task_id,
                    created_at=self.artifact_fallback.created_at,
                    kind="assistant_artifact_fallback",
                )
            )
        return messages


class ConversationMemoryBuilder:
    def __init__(
        self,
        *,
        storage,
        config: ConversationMemoryConfig | None = None,
        summary_generator: SummaryGenerator | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage = storage
        self._config = config or ConversationMemoryConfig.from_runtime_config()
        self._summary_generator = summary_generator
        self._now_fn = now_fn or datetime.utcnow

    async def build(self, request: OrchestrationRequest, *, account_id: str | None = None) -> ConversationMemoryContext:
        conversation = await self._storage.get_conversation(request.conversation_id)
        if conversation is None:
            return self._empty_context(request, fallback_reason="conversation_missing")
        if account_id is not None and conversation.account_id != account_id:
            raise PermissionError(f"Conversation does not belong to account: {request.conversation_id}")

        messages = await self._storage.list_messages_for_conversation(request.conversation_id)
        tasks = await self._storage.list_tasks_for_conversation(request.conversation_id)
        messages = sorted(messages, key=lambda item: (item.created_at or datetime.min, item.message_id))
        tasks_by_id = {task.task_id: task for task in tasks}
        current_user_message = request.current_user_message or request.user_message

        history_messages = [message for message in messages if message.message_id != request.root_message_id]
        latest_summary = await self._latest_valid_summary(request.conversation_id, conversation.account_id)
        history_messages = _messages_after_summary_boundary(history_messages, latest_summary)
        post_boundary_task_ids = {message.task_id or message.message_id for message in history_messages}
        turns = await self._build_turns(
            history_messages,
            tasks_by_id=tasks_by_id,
            current_task_id=request.task_id,
            include_artifact_task_ids=post_boundary_task_ids,
        )
        source_message_count = len(history_messages)
        resolved_user_message, resolution_metadata = self._resolve_user_message(
            current_user_message,
            turns,
            summary_text=latest_summary.summary_text if latest_summary is not None else None,
        )
        capability_summaries = self._capability_summaries_from_metadata(request.metadata)
        upload_summaries = self._upload_summaries_from_metadata(request.metadata)
        capability_summaries = (*capability_summaries, *upload_summaries)

        context = await self._compress(
            request=request,
            account_id=conversation.account_id,
            current_user_message=current_user_message,
            resolved_user_message=resolved_user_message,
            resolution_metadata=resolution_metadata,
            turns=turns,
            existing_summary=latest_summary,
            source_message_count=source_message_count,
            capability_summaries=capability_summaries,
        )
        return context

    async def _latest_valid_summary(
        self,
        conversation_id: str,
        account_id: str,
    ) -> ConversationMemorySummary | None:
        try:
            summary = await self._storage.get_latest_conversation_memory_summary(
                conversation_id,
                account_id=account_id,
            )
        except AttributeError:
            return None
        if summary is None:
            return None
        if summary.summary_version != SUMMARY_VERSION:
            return None
        if summary.compression_policy_version != COMPRESSION_POLICY_VERSION:
            return None
        return summary

    async def _build_turns(
        self,
        messages: list[Message],
        *,
        tasks_by_id: Mapping[str, Task],
        current_task_id: str,
        include_artifact_task_ids: set[str],
    ) -> list[_BusinessTurn]:
        turns_by_id: dict[str, _BusinessTurn] = {}
        task_ids_with_assistant_message: set[str] = set()
        for message in messages:
            task_id = message.task_id or message.message_id
            turn = turns_by_id.setdefault(task_id, _BusinessTurn(turn_id=task_id))
            task = tasks_by_id.get(task_id)
            if message.role == MessageRole.USER:
                if task is not None and message.message_id == task.root_message_id:
                    turn.root = message
                elif task_id == current_task_id:
                    turn.clarifications.append(message)
                elif task is not None:
                    turn.clarifications.append(message)
                else:
                    turn.root = message
            elif message.role == MessageRole.ASSISTANT:
                turn.assistant = message
                if message.task_id:
                    task_ids_with_assistant_message.add(message.task_id)

        for task in tasks_by_id.values():
            if task.status != TaskStatus.COMPLETED or task.task_id in task_ids_with_assistant_message:
                continue
            if task.task_id not in include_artifact_task_ids:
                continue
            turn = turns_by_id.setdefault(task.task_id, _BusinessTurn(turn_id=task.task_id))
            if turn.assistant is not None:
                continue
            text_artifact = await self._final_text_artifact(task.task_id)
            if text_artifact is not None:
                turn.artifact_fallback = text_artifact

        return sorted(turns_by_id.values(), key=lambda turn: (turn.created_at or datetime.min, turn.turn_id))

    async def _final_text_artifact(self, task_id: str) -> Artifact | None:
        try:
            artifacts = await self._storage.list_artifacts_for_task(task_id)
        except AttributeError:
            return None
        for artifact in artifacts:
            if str(artifact.artifact_type) == str(ArtifactType.TEXT) and artifact.storage_ref.strip():
                return artifact
        return None

    def _capability_summaries_from_metadata(self, metadata: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        raw_items = metadata.get("capability_summaries") or ()
        if not isinstance(raw_items, list | tuple):
            return ()
        return tuple(
            safe
            for item in raw_items
            if isinstance(item, Mapping)
            for safe in (ConversationMemorySafeAllowlist.project_capability_output(item),)
            if safe
        )

    def _upload_summaries_from_metadata(self, metadata: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        raw_items = metadata.get("uploaded_artifacts") or ()
        if not isinstance(raw_items, list | tuple):
            return ()
        projected = []
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            safe = ConversationMemorySafeAllowlist.project_upload_summary(item)
            if safe:
                projected.append({"upload": safe})
        return tuple(projected)

    async def _compress(
        self,
        *,
        request: OrchestrationRequest,
        account_id: str,
        current_user_message: str,
        resolved_user_message: str | None,
        resolution_metadata: Mapping[str, Any],
        turns: list[_BusinessTurn],
        existing_summary: ConversationMemorySummary | None,
        source_message_count: int,
        capability_summaries: tuple[dict[str, Any], ...],
    ) -> ConversationMemoryContext:
        token_budget = self._config.actual_memory_budget
        all_recent_messages = tuple(message for turn in turns for message in turn.memory_messages())
        existing_summary_text = existing_summary.summary_text if existing_summary is not None else None
        estimated_before = _estimate_context_tokens(
            all_recent_messages,
            existing_summary_text,
            capability_summaries,
            current_user_message,
            resolved_user_message,
        )
        compression_level = "none"
        history_summary: str | None = existing_summary_text
        fallback_reason: str | None = None
        truncated = False
        recent_messages = all_recent_messages

        if estimated_before > token_budget:
            compression_level = "level_1"
            kept_turns = turns[-self._config.recent_turns :] if self._config.recent_turns > 0 else []
            older_turns = turns[: max(0, len(turns) - len(kept_turns))]
            recent_messages = tuple(message for turn in kept_turns for message in turn.memory_messages())
            if older_turns:
                if self._config.enable_summary_llm and self._summary_generator is not None:
                    prompt = self._build_summary_prompt(older_turns, existing_summary_text=existing_summary_text)
                    try:
                        generated = self._summary_generator(prompt)
                        if inspect.isawaitable(generated):
                            generated = await generated
                        history_summary = str(generated or "").strip()[: self._config.effective_summary_max_tokens * 4]
                        if history_summary:
                            compression_level = "level_2"
                            await self._save_summary(
                                request=request,
                                account_id=account_id,
                                summary_text=history_summary,
                                older_turns=older_turns,
                                existing_summary=existing_summary,
                            )
                        else:
                            history_summary = existing_summary_text
                            compression_level = "fallback"
                            fallback_reason = "summary_empty"
                    except Exception:
                        history_summary = existing_summary_text
                        compression_level = "fallback"
                        fallback_reason = "summary_llm_failed"
                else:
                    compression_level = "fallback"
                    fallback_reason = "summary_llm_disabled" if not self._config.enable_summary_llm else "summary_llm_unavailable"
                if compression_level == "fallback":
                    truncated = True

        estimated_after = _estimate_context_tokens(recent_messages, history_summary, capability_summaries, current_user_message, resolved_user_message)
        return ConversationMemoryContext(
            conversation_id=request.conversation_id,
            root_message_id=request.root_message_id,
            source_message_count=source_message_count,
            current_user_message=current_user_message,
            resolved_user_message=resolved_user_message,
            recent_messages=recent_messages,
            clarification_messages=tuple(message for message in recent_messages if message.kind == "clarification"),
            history_summary=history_summary,
            capability_summaries=capability_summaries,
            compression_level=compression_level,
            token_budget=token_budget,
            estimated_tokens_before=estimated_before,
            estimated_tokens_after=estimated_after,
            truncated=truncated or estimated_after > token_budget,
            fallback_reason=fallback_reason,
            resolution_metadata=resolution_metadata,
        )

    def _build_summary_prompt(self, older_turns: list[_BusinessTurn], *, existing_summary_text: str | None) -> str:
        items = [message.to_prompt_dict() for turn in older_turns for message in turn.memory_messages()]
        existing = (
            "已有历史摘要如下，请把它与新增较早对话增量合并；若有冲突，以新增原文和用户纠正为准。\n"
            + existing_summary_text
            + "\n\n"
            if existing_summary_text
            else ""
        )
        return (
            "请将以下较早对话压缩为忠实摘要。只保留用户目标、已确认实体、关键约束、已给出的结论、未完成事项和用户纠正信息；不得引入新事实。\n"
            + existing
            + json.dumps(items, ensure_ascii=False, indent=2, default=str)
        )

    async def _save_summary(
        self,
        *,
        request: OrchestrationRequest,
        account_id: str,
        summary_text: str,
        older_turns: list[_BusinessTurn],
        existing_summary: ConversationMemorySummary | None,
    ) -> None:
        if not hasattr(self._storage, "save_conversation_memory_summary"):
            return
        messages = [message for turn in older_turns for message in turn.memory_messages()]
        if not messages:
            return
        last = messages[-1]
        now = self._now_fn()
        await self._storage.save_conversation_memory_summary(
            ConversationMemorySummary(
                summary_id=f"memory-summary-{uuid4().hex}",
                conversation_id=request.conversation_id,
                account_id=account_id,
                covered_until_turn_id=older_turns[-1].turn_id,
                covered_until_message_id=last.message_id,
                covered_until_created_at=last.created_at,
                summary_text=summary_text,
                source_message_count=len(messages)
                + (existing_summary.source_message_count if existing_summary is not None else 0),
                source_message_ids_hash=_message_ids_hash(
                    [
                        *((existing_summary.source_message_ids_hash,) if existing_summary is not None else ()),
                        *(message.message_id for message in messages),
                    ]
                ),
                estimated_tokens=get_num_of_tokens_from_messages([summary_text]),
                summary_version=SUMMARY_VERSION,
                compression_policy_version=COMPRESSION_POLICY_VERSION,
                model_metadata_safe={"provider": "conversation_memory_summary_generator"},
                created_at=now,
                updated_at=now,
            )
        )

    def _resolve_user_message(
        self,
        current_user_message: str,
        turns: list[_BusinessTurn],
        *,
        summary_text: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        current = current_user_message.strip()
        if not current:
            return None, {"resolved": False, "reason": "empty_current_message"}
        current_entity = _extract_variety_entity(current)
        if current_entity:
            return None, {"resolved": False, "reason": "current_message_has_entity", "entity": current_entity}
        entity = _last_entity_from_turns(turns)
        strategy = "deterministic_entity_reference"
        if not entity and summary_text:
            entity = _extract_variety_entity(summary_text)
            strategy = "deterministic_summary_entity_reference"
        if not entity:
            return None, {"resolved": False, "reason": "no_history_entity"}
        if any(token in current for token in ("它", "这个", "该品种", "这个品种", "换成", "不是这个", "继续")):
            resolved = _compose_resolved_question(current, entity)
            return resolved, {"resolved": True, "strategy": strategy, "entity": entity}
        return None, {"resolved": False, "reason": "no_reference_signal", "entity": entity}

    def _empty_context(self, request: OrchestrationRequest, *, fallback_reason: str) -> ConversationMemoryContext:
        return ConversationMemoryContext(
            conversation_id=request.conversation_id,
            root_message_id=request.root_message_id,
            source_message_count=0,
            current_user_message=request.current_user_message or request.user_message,
            resolved_user_message=request.resolved_user_message,
            compression_level="fallback",
            token_budget=self._config.actual_memory_budget,
            fallback_reason=fallback_reason,
        )


def effective_user_message(request: OrchestrationRequest) -> str:
    resolved = (request.resolved_user_message or "").strip()
    return resolved or (request.current_user_message or request.user_message)


def _messages_after_summary_boundary(
    messages: list[Message],
    summary: ConversationMemorySummary | None,
) -> list[Message]:
    if summary is None:
        return messages
    if summary.covered_until_message_id:
        for index, message in enumerate(messages):
            if message.message_id == summary.covered_until_message_id:
                return messages[index + 1 :]
    if summary.covered_until_created_at is None:
        return messages
    return [
        message
        for message in messages
        if message.created_at is None or message.created_at > summary.covered_until_created_at
    ]


def sanitize_memory_prompt_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    allowed: dict[str, Any] = {}
    if raw.get("history_summary"):
        allowed["history_summary"] = str(raw["history_summary"])
    for key in ("current_user_message", "resolved_user_message", "compression_level", "token_budget", "estimated_tokens_before", "estimated_tokens_after", "truncated", "fallback_reason"):
        if key in raw and raw[key] not in (None, ""):
            allowed[key] = raw[key]
    for key in ("recent_messages", "clarification_messages"):
        value = raw.get(key)
        if isinstance(value, list | tuple):
            allowed[key] = [_sanitize_memory_message(item) for item in value if isinstance(item, Mapping)]
    summaries = raw.get("capability_summaries")
    if isinstance(summaries, list | tuple):
        safe_summaries: list[dict[str, Any]] = []
        for item in summaries:
            if not isinstance(item, Mapping):
                continue
            if isinstance(item.get("upload"), Mapping):
                upload = ConversationMemorySafeAllowlist.project_upload_summary(item["upload"])
                if upload:
                    safe_summaries.append({"upload": upload})
                continue
            projected = ConversationMemorySafeAllowlist.project_capability_output(item)
            if projected:
                safe_summaries.append(projected)
        if safe_summaries:
            allowed["capability_summaries"] = safe_summaries
    resolution = raw.get("resolution_metadata")
    if isinstance(resolution, Mapping):
        allowed["resolution_metadata"] = _json_safe_mapping({key: value for key, value in resolution.items() if key in {"resolved", "strategy", "reason", "entity"}})
    return _strip_none(allowed)


def _sanitize_memory_message(item: Mapping[str, Any]) -> dict[str, Any]:
    return _json_safe_mapping(
        {
            key: item[key]
            for key in ("message_id", "role", "content", "task_id", "kind", "created_at")
            if key in item and item[key] is not None
        }
    )


def _compose_resolved_question(current: str, entity: str) -> str:
    replacement = _extract_variety_entity(current)
    target = replacement or entity
    if "基因" in current or "基因型" in current:
        return f"查询{target}的基因型信息"
    if "审定" in current:
        return f"查询{target}的审定信息"
    if "换成" in current or "不是这个" in current:
        return f"将上一轮查询对象替换为{target}并继续查询相关信息"
    return f"围绕{target}继续回答：{current}"


def _last_entity_from_turns(turns: list[_BusinessTurn]) -> str | None:
    for turn in reversed(turns):
        for message in reversed(turn.memory_messages()):
            entity = _extract_variety_entity(message.content)
            if entity:
                return entity
    return None


def _extract_variety_entity(text: str) -> str | None:
    # Conservative agricultural variety patterns for names like 龙粳33 / 龙粳18号.
    patterns = (
        r"[\u4e00-\u9fff]{1,2}(?:粳|稻|麦|豆|棉|薯|玉|油|粱|早)\d{1,4}号?",
        r"[\u4e00-\u9fff]{1,6}\d{1,4}号?",
    )
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for candidate in reversed(matches):
            normalized = _strip_entity_prefix(candidate)
            if normalized:
                return normalized
    return None


def _strip_entity_prefix(candidate: str) -> str:
    if _looks_like_non_entity_number(candidate):
        return ""
    prefixes = (
        "帮我查询",
        "帮我查",
        "查询品种",
        "查询",
        "查一下",
        "查下",
        "查",
        "品种",
        "一下",
    )
    leading_noise = {"下", "过", "种"}
    normalized = candidate
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if normalized.startswith(prefix) and len(normalized) > len(prefix):
                normalized = normalized[len(prefix) :]
                changed = True
                break
        while len(normalized) > 3 and normalized[0] in leading_noise:
            normalized = normalized[1:]
            changed = True
    return normalized


def _looks_like_non_entity_number(candidate: str) -> bool:
    non_entity_prefixes = (
        "要求",
        "需要",
        "设置",
        "使用",
        "按照",
        "重复",
        "区组",
        "次数",
        "第",
        "共",
    )
    prefixes = "|".join(re.escape(prefix) for prefix in non_entity_prefixes)
    return bool(re.fullmatch(rf"(?:{prefixes})\d{{1,4}}号?", candidate))


def _estimate_context_tokens(
    messages: Iterable[ConversationMemoryMessage],
    history_summary: str | None,
    capability_summaries: Iterable[Mapping[str, Any]],
    current_user_message: str,
    resolved_user_message: str | None,
) -> int:
    parts = [message.content for message in messages]
    if history_summary:
        parts.append(history_summary)
    parts.append(current_user_message)
    if resolved_user_message:
        parts.append(resolved_user_message)
    parts.extend(json.dumps(dict(summary), ensure_ascii=False, sort_keys=True, default=str) for summary in capability_summaries)
    try:
        return get_num_of_tokens_from_messages(parts)
    except Exception:
        return max(1, sum(len(part) for part in parts) // 2)


def _message_ids_hash(message_ids: Iterable[str]) -> str:
    joined = "\n".join(message_ids)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _json_safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(value), ensure_ascii=False, default=str))


def _strip_none(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _coerce_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return bool(value)

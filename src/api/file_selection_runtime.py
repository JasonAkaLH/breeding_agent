from __future__ import annotations

import json
import hashlib
import re
from dataclasses import replace
from typing import Any, Mapping

from src.api.dto import SubmitMessageRequest
from src.api.file_selection import (
    FileRequirementProfile,
    FileRequirementProfileError,
    FileSelectionAnswerResolver,
    FileSelectionDecision,
    FileSelectionTriggerDetector,
    build_recent_usage,
    candidate_from_resource,
    deterministic_file_decision,
    parse_selector_decision,
    render_file_selection_question,
)
from src.orchestration.visible_message_history import persist_interrupt_question_message
from src.core.enums import EventVisibility, InterruptStatus, MessageRole, NodeCriticality, NodeStatus, TaskStatus
from src.core.models import Interrupt, InterruptAnswer, Message, Task, TaskNode
from src.orchestration.models import OrchestrationRequest


class ConversationFileSelectionRuntimeMixin:
    _AUDIT_SENSITIVE_TEXT_RE = re.compile(
        r"("
        r"secret|token|password|passwd|credential|"
        r"api[_-]?key|apikey|access[_-]?key|private[_-]?key|authorization|bearer|"
        r"storage_key|mount_path|content_base64|content|path|"
        r"/tmp/|/var/|/users/|[A-Za-z]:\\"
        r")",
        re.IGNORECASE,
    )

    async def _maybe_handle_conversation_file_selection(
        self,
        *,
        task: Task,
        username: str,
        request: SubmitMessageRequest,
        metadata: dict[str, Any],
        requested_capability_id: str | None = None,
        continued_pending_context: Any | None = None,
        explicit_upload_ids: tuple[str, ...] = (),
    ) -> bool:
        mode = self._conversation_file_selector_mode
        if mode == "disabled":
            return False
        profile = self._file_requirement_profile_for_request(
            request,
            metadata=metadata,
            requested_capability_id=requested_capability_id,
            continued_pending_context=continued_pending_context,
        )
        resources = await self.storage.list_conversation_file_resources(task.conversation_id, username, include_deleted=False)
        trigger, trigger_reason = FileSelectionTriggerDetector().should_trigger(
            text=request.content,
            profile=profile,
            has_explicit_uploads=bool(explicit_upload_ids),
            active_file_count=len(resources),
        )
        if not trigger:
            return False
        attachments = await self.storage.list_task_input_attachments_for_conversation(task.conversation_id, limit=100)
        recent_usage = build_recent_usage(attachments)
        candidates = tuple(
            candidate_from_resource(resource, recent_usage=recent_usage.get(resource.file_id))
            for resource in resources
            if resource.status != "deleted"
        )
        await self._record_file_selection_audit_event(
            task=task,
            event_type="conversation_file.file_selector_invoked",
            payload=self._file_selection_invoked_payload(
                mode=mode,
                trigger_reason=trigger_reason,
                profile=profile,
                candidates=candidates,
            ),
        )
        decision = (
            FileSelectionDecision("no_usable_file", reason_code="no_files_in_conversation")
            if trigger_reason == "no_files_in_conversation"
            else await self._conversation_file_selection_decision(
                text=request.content,
                profile=profile,
                candidates=candidates,
                metadata=metadata,
            )
        )
        if self._is_invalid_selector_output_decision(decision):
            await self._record_file_selection_audit_event(
                task=task,
                event_type="conversation_file.file_selector_invalid_output",
                payload=self._file_selection_invalid_output_payload(
                    mode=mode,
                    trigger_reason=trigger_reason,
                    profile=profile,
                    candidates=candidates,
                    decision=decision,
                ),
            )
        await self._record_file_selection_audit_event(
            task=task,
            event_type="conversation_file.file_selector_decision_recorded",
            payload=self._file_selection_decision_payload(
                mode=mode,
                trigger_reason=trigger_reason,
                profile=profile,
                candidates=candidates,
                decision=decision,
            ),
        )
        if mode == "shadow":
            return False
        if decision.decision == "no_file_needed":
            return False
        if decision.decision == "select_one":
            await self._bind_file_selection_uploads_or_open_sheet_selection(
                task=task,
                username=username,
                upload_ids=decision.upload_ids,
                metadata=metadata,
                source_message_id=task.root_message_id,
            )
            if await self._has_open_interrupt(task.task_id):
                return True
            metadata.update(await self._conversation_file_context_metadata_for_task(task))
            await self._record_file_selection_audit_event(
                task=task,
                event_type="conversation_file.file_selector_auto_bound",
                payload={"selected_upload_ids": list(decision.upload_ids), "source": "submit_message"},
            )
            return False
        await self._open_file_selection_interrupt(
            task=task,
            metadata=metadata,
            profile=profile,
            candidates=candidates,
            reason_code=decision.reason_code or decision.decision,
        )
        return True

    async def _conversation_file_selection_decision(
        self,
        *,
        text: str,
        profile: FileRequirementProfile,
        candidates: tuple[Any, ...],
        metadata: Mapping[str, Any],
    ) -> FileSelectionDecision:
        decision = deterministic_file_decision(text=text, profile=profile, candidates=candidates)
        if decision.decision == "select_one" or self._skill_input_text_generator is None:
            return decision
        prompt = json.dumps(
            {
                "mode": "conversation_file_selector",
                "instructions": [
                    "Choose files only from candidate upload_ids.",
                    "Use only prompt-safe metadata. File bodies and storage paths are unavailable.",
                    "Return JSON: decision, upload_ids, confidence, reason_code.",
                    "Use ambiguous when unsure.",
                ],
                "user_message": text,
                "profile": {
                    "source": profile.source,
                    "required": profile.required,
                    "allow_multiple": profile.allow_multiple,
                    "expected_content": list(profile.expected_content),
                    "supported_file_types": list(profile.supported_file_types),
                    "helpful_columns": list(profile.helpful_columns),
                    "disambiguation_hint": profile.disambiguation_hint,
                    "user_file_reference": profile.user_file_reference,
                },
                "candidates": [candidate.to_prompt_safe_dict() for candidate in candidates],
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        raw = await self._call_skill_input_text_generator(
            prompt,
            metadata={**dict(metadata), "llm_call_site": "conversation_file_selector"},
            reasoning_context={"stage": "conversation_file_selector"},
        )
        if not raw:
            return decision
        parsed = parse_selector_decision(
            raw,
            candidates=candidates,
            profile=profile,
            allow_guarded_multi_select=self._conversation_file_selector_guarded_multi_select,
        )
        if parsed.reason_code in {"empty_selector_output", "invalid_json", "invalid_shape", "unknown_decision", "unknown_upload_id", "cardinality_mismatch"}:
            return FileSelectionDecision(
                parsed.decision,
                upload_ids=parsed.upload_ids,
                confidence=parsed.confidence,
                reason_code=parsed.reason_code,
                question=parsed.question,
                raw={"_selector_invalid_output": True},
            )
        return parsed

    def _file_requirement_profile_for_request(
        self,
        request: SubmitMessageRequest,
        *,
        metadata: Mapping[str, Any] | None = None,
        requested_capability_id: str | None = None,
        continued_pending_context: Any | None = None,
    ) -> FileRequirementProfile:
        resolved_metadata = dict(metadata or request.metadata)
        if "file_intent" in resolved_metadata:
            raise FileRequirementProfileError("metadata.file_intent is not supported; use final file_selection fields")
        for key in ("file_requirement_profile", "file_selection"):
            value = resolved_metadata.get(key)
            if isinstance(value, Mapping):
                profile = FileRequirementProfile.from_mapping(value, source="metadata")
                if profile.is_meaningful():
                    return profile
        soft_binding = resolved_metadata.get("soft_skill_binding")
        if soft_binding is None:
            soft_binding = self._normalize_soft_skill_binding(request.metadata)
        if isinstance(soft_binding, Mapping):
            if "file_intent" in soft_binding:
                raise FileRequirementProfileError(
                    "metadata.soft_skill_binding.file_intent is not supported; use final file_selection fields"
                )
            for key in ("file_requirement_profile", "file_selection"):
                value = soft_binding.get(key)
                if isinstance(value, Mapping):
                    profile = FileRequirementProfile.from_mapping(value, source="soft_skill_binding")
                    if profile.is_meaningful():
                        return profile
        pending_capability_id = self._metadata_text(getattr(continued_pending_context, "capability_id", ""))
        for capability_id, source in (
            (pending_capability_id, "skill_contract"),
            (requested_capability_id, "skill_contract"),
        ):
            profile = self._file_requirement_profile_for_capability(capability_id, source=source)
            if profile is not None and profile.is_meaningful():
                return profile
        trigger, _ = FileSelectionTriggerDetector().should_trigger(
            text=request.content,
            profile=FileRequirementProfile(),
            has_explicit_uploads=False,
            active_file_count=1,
        )
        return FileRequirementProfile(source="user_query", user_file_reference=request.content if trigger else "")

    def _file_requirement_profile_for_capability(self, capability_id: str | None, *, source: str) -> FileRequirementProfile | None:
        capability_id = self._metadata_text(capability_id)
        if not capability_id or not capability_id.startswith("skill.") or self._skill_runtime_state is None:
            return None
        contract = self._skill_runtime_state.active_bundle.contract_by_capability_id.get(capability_id)
        if contract is None:
            return None
        metadata = self._soft_skill_file_selection_metadata(contract, source=source)
        raw_profile = metadata.get("file_selection") if isinstance(metadata, Mapping) else None
        if not isinstance(raw_profile, Mapping):
            return None
        profile = FileRequirementProfile.from_mapping(raw_profile, source=source)
        contract_selection = getattr(contract, "file_selection", None)
        contract_has_selection = bool(contract_selection and any((
            getattr(contract_selection, "required", False),
            getattr(contract_selection, "allow_multiple", False),
            getattr(contract_selection, "expected_content", ()),
            getattr(contract_selection, "supported_file_types", ()),
            getattr(contract_selection, "helpful_columns", ()),
            getattr(contract_selection, "disambiguation_hint", ""),
        )))
        if profile.source == "skill_contract" and not contract_has_selection:
            return FileRequirementProfile(
                source="input_schema",
                required=profile.required,
                allow_multiple=profile.allow_multiple,
                expected_content=profile.expected_content,
                supported_file_types=profile.supported_file_types,
                helpful_columns=profile.helpful_columns,
                disambiguation_hint=profile.disambiguation_hint,
                user_file_reference=profile.user_file_reference,
                context_notes=profile.context_notes,
            )
        return profile

    def _file_selection_invoked_payload(
        self,
        *,
        mode: str,
        trigger_reason: str,
        profile: FileRequirementProfile,
        candidates: tuple[Any, ...],
    ) -> dict[str, Any]:
        candidate_summaries = self._candidate_audit_summaries(candidates)
        return {
            "mode": mode,
            "trigger_reason": trigger_reason,
            "requirement_profile": self._profile_audit_summary(profile),
            "candidate_count": len(candidates),
            "candidate_upload_ids": [candidate.upload_id for candidate in candidates],
            "candidate_hash": self._candidate_audit_hash(candidate_summaries),
            "candidates": candidate_summaries,
        }

    def _file_selection_decision_payload(
        self,
        *,
        mode: str,
        trigger_reason: str,
        profile: FileRequirementProfile,
        candidates: tuple[Any, ...],
        decision: FileSelectionDecision,
    ) -> dict[str, Any]:
        candidate_summaries = self._candidate_audit_summaries(candidates)
        return {
            "mode": mode,
            "trigger_reason": trigger_reason,
            "requirement_profile": self._profile_audit_summary(profile),
            "candidate_count": len(candidates),
            "candidate_upload_ids": [candidate.upload_id for candidate in candidates],
            "candidate_hash": self._candidate_audit_hash(candidate_summaries),
            "decision": decision.decision,
            "reason_code": decision.reason_code,
            "confidence": decision.confidence,
            "selected_upload_ids": list(decision.upload_ids),
            "would_auto_bind": decision.decision in {"select_one", "select_many"},
            "would_clarify": decision.decision in {"ambiguous", "no_usable_file"},
        }

    def _file_selection_invalid_output_payload(
        self,
        *,
        mode: str,
        trigger_reason: str,
        profile: FileRequirementProfile,
        candidates: tuple[Any, ...],
        decision: FileSelectionDecision,
    ) -> dict[str, Any]:
        return {
            "mode": mode,
            "trigger_reason": trigger_reason,
            "requirement_profile": self._profile_audit_summary(profile),
            "candidate_count": len(candidates),
            "candidate_upload_ids": [candidate.upload_id for candidate in candidates],
            "decision": decision.decision,
            "reason_code": decision.reason_code,
            "confidence": decision.confidence,
            "would_auto_bind": False,
            "would_clarify": True,
        }

    @staticmethod
    def _profile_audit_summary(profile: FileRequirementProfile) -> dict[str, Any]:
        return {
            "source": profile.source,
            "required": profile.required,
            "allow_multiple": profile.allow_multiple,
            "expected_content": ConversationFileSelectionRuntimeMixin._safe_audit_text_tuple(profile.expected_content),
            "supported_file_types": ConversationFileSelectionRuntimeMixin._safe_audit_text_tuple(profile.supported_file_types),
            "helpful_columns": ConversationFileSelectionRuntimeMixin._safe_audit_text_tuple(profile.helpful_columns),
            "disambiguation_hint": ConversationFileSelectionRuntimeMixin._safe_audit_text(profile.disambiguation_hint),
            "has_user_file_reference": bool(profile.user_file_reference),
            "context_notes": ConversationFileSelectionRuntimeMixin._safe_audit_text_tuple(profile.context_notes),
        }

    @staticmethod
    def _safe_audit_text_tuple(values: tuple[str, ...]) -> list[str]:
        safe: list[str] = []
        for value in values:
            text = ConversationFileSelectionRuntimeMixin._safe_audit_text(value)
            if text:
                safe.append(text)
        return safe

    @staticmethod
    def _safe_audit_text(value: str) -> str:
        text = str(value or "").strip()
        if not text or ConversationFileSelectionRuntimeMixin._AUDIT_SENSITIVE_TEXT_RE.search(text):
            return ""
        return text[:120]

    @staticmethod
    def _candidate_audit_summaries(candidates: tuple[Any, ...]) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for candidate in candidates:
            safe = dict(candidate.to_prompt_safe_dict())
            safe.pop("description_summary", None)
            summaries.append(safe)
        return summaries

    @staticmethod
    def _candidate_audit_hash(candidate_summaries: list[dict[str, Any]]) -> str:
        payload = json.dumps(candidate_summaries, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _is_invalid_selector_output_decision(decision: FileSelectionDecision) -> bool:
        return isinstance(decision.raw, Mapping) and bool(decision.raw.get("_selector_invalid_output"))

    async def _bind_file_selection_uploads_or_open_sheet_selection(
        self,
        *,
        task: Task,
        username: str,
        upload_ids: tuple[str, ...],
        metadata: dict[str, Any],
        source_message_id: str | None = None,
        interrupt_answer_id: str | None = None,
    ) -> None:
        upload_context = await self.resolve_uploads_for_message(task.conversation_id, username, list(upload_ids))
        self._raise_missing_uploads(upload_context.get("missing_upload_ids"), context="file selection")
        if upload_context.get("pending_sheet_selections"):
            await self._open_sheet_selection_interrupt(
                task=task,
                metadata=metadata,
                pending_sheet_selections=upload_context["pending_sheet_selections"],
            )
            return
        await self._bind_or_update_resume_input_attachments(
            task=task,
            username=username,
            upload_ids=list(upload_ids),
            source_kind="file_selector",
            source_message_id=source_message_id or task.root_message_id,
            interrupt_answer_id=interrupt_answer_id,
        )

    async def _open_file_selection_interrupt(
        self,
        *,
        task: Task,
        metadata: Mapping[str, Any],
        profile: FileRequirementProfile,
        candidates: tuple[Any, ...],
        reason_code: str,
    ) -> None:
        now = self._utcnow_naive()
        node_id = f"{task.task_id}:file_selection"
        node = TaskNode(
            node_id=node_id,
            task_id=task.task_id,
            capability_id=task.requested_capability_id or "main_agent.respond",
            status=NodeStatus.RUNNING,
            criticality=NodeCriticality.REQUIRED,
            started_at=now,
        )
        await self.storage.save_task_node(node)
        await self.storage.save_task(
            replace(task, status=TaskStatus.RUNNING, root_node_id=task.root_node_id or node_id, updated_at=now)
        )
        required_fields = {
            "_file_selection": {
                "presentation": "natural_language",
                "reason_code": reason_code,
                "candidate_upload_ids": [candidate.upload_id for candidate in candidates],
                "candidates": [candidate.to_prompt_safe_dict() for candidate in candidates],
                "profile": {
                    "source": profile.source,
                    "required": profile.required,
                    "allow_multiple": profile.allow_multiple,
                    "expected_content": list(profile.expected_content),
                    "supported_file_types": list(profile.supported_file_types),
                    "helpful_columns": list(profile.helpful_columns),
                    "disambiguation_hint": profile.disambiguation_hint,
                },
            },
            "file_selection_answer": {
                "type": "string",
                "description": "请说明要使用哪个文件，或回复不用文件。",
            },
            "replacement_file": {
                "type": "artifact",
                "accepts_upload": True,
                "required": False,
                "description": "也可以重新上传要使用的文件。",
            },
        }
        interrupt = Interrupt(
            interrupt_id=f"{node_id}:interrupt:file_selection_ambiguous",
            conversation_id=task.conversation_id,
            task_id=task.task_id,
            node_id=node_id,
            source_agent=task.requested_capability_id or "main_agent.respond",
            source_message_id=task.root_message_id,
            question=render_file_selection_question(candidates, reason_code=reason_code),
            reason_code="file_selection_ambiguous",
            required_fields=required_fields,
        )
        self._task_file_selection_resume_metadata[task.task_id] = dict(metadata)
        saved_interrupt = await self.interrupt_service.open_interrupt(interrupt, now=now)
        await persist_interrupt_question_message(self.storage, saved_interrupt, created_at=now)
        await self._record_file_selection_audit_event(
            task=task,
            event_type="conversation_file.file_selector_clarification_requested",
            payload={"reason_code": reason_code, "candidate_count": len(candidates)},
        )
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                node_id=node_id,
                event_type="node.waiting_for_input",
                payload={
                    "reason": saved_interrupt.reason_code,
                    "reason_code": saved_interrupt.reason_code,
                    "interrupt_id": saved_interrupt.interrupt_id,
                },
                created_at=now,
            )
        )

    async def _record_file_selection_audit_event(self, *, task: Task, event_type: str, payload: Mapping[str, Any]) -> None:
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                event_type=event_type,
                payload=dict(payload),
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )

    async def _has_open_interrupt(self, task_id: str) -> bool:
        return any(interrupt.status == InterruptStatus.OPEN for interrupt in await self.storage.list_interrupts_for_task(task_id))

    async def _answer_file_selection_interrupt(
        self,
        *,
        task: Task,
        interrupt: Interrupt,
        answer_payload: dict[str, object],
        source_message_id: str | None = None,
        request_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, object]:
        conversation = await self.storage.get_conversation(task.conversation_id)
        if conversation is None:
            raise ValueError(f"Unknown conversation: {task.conversation_id}")
        raw_answer = answer_payload.get("answer")
        answer_text = str(answer_payload.get("file_selection_answer") or answer_payload.get("answer") or "").strip()
        upload_ids: tuple[str, ...] = ()
        if isinstance(raw_answer, Mapping):
            answer_text = str(raw_answer.get("text") or answer_text or "").strip()
            upload_ids = self._normalize_upload_ids(raw_answer.get("upload_ids") or ())
        elif answer_payload.get("upload_ids"):
            upload_ids = self._normalize_upload_ids(answer_payload.get("upload_ids") or ())

        file_selection = interrupt.required_fields.get("_file_selection")
        file_selection_context = dict(file_selection) if isinstance(file_selection, Mapping) else {}
        candidate_ids = [
            str(item).strip()
            for item in file_selection_context.get("candidate_upload_ids", [])
            if str(item).strip()
        ]
        resources = await self.storage.list_conversation_file_resources(task.conversation_id, conversation.username, include_deleted=False)
        candidate_resources = [resource for resource in resources if not candidate_ids or resource.file_id in candidate_ids]
        recent_usage = build_recent_usage(await self.storage.list_task_input_attachments_for_conversation(task.conversation_id, limit=100))
        candidates = tuple(
            candidate_from_resource(resource, recent_usage=recent_usage.get(resource.file_id))
            for resource in candidate_resources
            if resource.status != "deleted"
        )
        profile = FileRequirementProfile.from_mapping(
            file_selection_context.get("profile") if isinstance(file_selection_context.get("profile"), Mapping) else {},
            source="interrupt",
        )
        decision = FileSelectionAnswerResolver().resolve(
            answer_text,
            candidates,
            replacement_upload_ids=upload_ids,
            allow_multiple=profile.allow_multiple,
        )
        if decision.decision == "no_file_needed" and profile.required:
            decision = FileSelectionDecision("ambiguous", reason_code="required_file_cannot_be_skipped")
        if decision.decision == "no_file_needed":
            resume_metadata = {
                **self._task_file_selection_resume_metadata.get(task.task_id, {}),
                **self._resume_skill_revision_metadata(task.task_id),
                **await self._resume_llm_metadata(task, request_metadata),
            }
            answer = InterruptAnswer(
                interrupt_answer_id=self._make_id("interrupt-answer"),
                interrupt_id=interrupt.interrupt_id,
                answer_payload=dict(answer_payload),
                source_message_id=source_message_id or str(answer_payload.get("client_request_id") or self._make_id("msg")),
                created_at=self._utcnow_naive(),
            )
            saved_interrupt = await self.interrupt_service.record_answer(answer)
            await self.storage.save_message(
                Message(
                    message_id=answer.source_message_id,
                    conversation_id=task.conversation_id,
                    role=MessageRole.USER,
                    content=answer_text or self._format_answer_message(answer_payload),
                    task_id=task.task_id,
                    created_at=self._utcnow_naive(),
                )
            )
            await self._schedule_execution(
                OrchestrationRequest(
                    task_id=task.task_id,
                    conversation_id=task.conversation_id,
                    root_message_id=task.root_message_id,
                    user_message=task.summary or "",
                    requested_capability_id=task.requested_capability_id,
                    metadata=resume_metadata,
                )
            )
            self._task_file_selection_resume_metadata.pop(task.task_id, None)
            await self._record_file_selection_audit_event(
                task=task,
                event_type="conversation_file.file_selector_resumed_from_interrupt",
                payload={"decision": decision.decision, "reason_code": decision.reason_code},
            )
            return {
                "interrupt_id": saved_interrupt.interrupt_id,
                "status": str(saved_interrupt.status),
                "node_id": saved_interrupt.node_id,
                "answer_payload": dict(answer_payload),
                "action": "resumed",
                "source_message_id": answer.source_message_id,
            }
        if decision.decision not in {"select_one", "select_many"}:
            assistant_text = render_file_selection_question(candidates, reason_code=decision.reason_code)
            assistant_message = Message(
                message_id=self._make_id("msg"),
                conversation_id=task.conversation_id,
                role=MessageRole.ASSISTANT,
                content=assistant_text,
                task_id=task.task_id,
                stream_status="interrupt_visible",
                created_at=self._utcnow_naive(),
            )
            await self.storage.save_message(assistant_message)
            await self._record_file_selection_audit_event(
                task=task,
                event_type="conversation_file.file_selector_invalid_output",
                payload={"reason_code": decision.reason_code},
            )
            return {
                "interrupt_id": interrupt.interrupt_id,
                "status": str(interrupt.status),
                "node_id": interrupt.node_id,
                "answer_payload": dict(answer_payload),
                "action": "clarification_answer",
                "assistant_message": assistant_text,
                "source_message_id": source_message_id or str(answer_payload.get("client_request_id") or ""),
            }

        resume_metadata = {
            **self._task_file_selection_resume_metadata.get(task.task_id, {}),
            **self._resume_skill_revision_metadata(task.task_id),
            **await self._resume_llm_metadata(task, request_metadata),
        }
        selected_answer_payload = dict(answer_payload)
        selected_answer_payload["upload_ids"] = list(decision.upload_ids)
        if isinstance(selected_answer_payload.get("answer"), Mapping):
            nested_answer = dict(selected_answer_payload["answer"])
            nested_answer["upload_ids"] = list(decision.upload_ids)
            selected_answer_payload["answer"] = nested_answer
        answer = InterruptAnswer(
            interrupt_answer_id=self._make_id("interrupt-answer"),
            interrupt_id=interrupt.interrupt_id,
            answer_payload=selected_answer_payload,
            source_message_id=source_message_id or str(answer_payload.get("client_request_id") or self._make_id("msg")),
            created_at=self._utcnow_naive(),
        )
        await self._bind_file_selection_uploads_or_open_sheet_selection(
            task=task,
            username=conversation.username,
            upload_ids=decision.upload_ids,
            metadata=resume_metadata,
            source_message_id=answer.source_message_id,
            interrupt_answer_id=answer.interrupt_answer_id,
        )
        resume_metadata.update(await self._conversation_file_context_metadata_for_task(task))
        saved_interrupt = await self.interrupt_service.record_answer(answer)
        await self.storage.save_message(
            Message(
                message_id=answer.source_message_id,
                conversation_id=task.conversation_id,
                role=MessageRole.USER,
                content=answer_text or self._format_answer_message(selected_answer_payload),
                task_id=task.task_id,
                created_at=self._utcnow_naive(),
            )
        )
        await self._record_file_selection_audit_event(
            task=task,
            event_type="conversation_file.file_selector_resumed_from_interrupt",
            payload={
                "decision": decision.decision,
                "reason_code": decision.reason_code,
                "selected_upload_ids": list(decision.upload_ids),
            },
        )
        if await self._has_open_interrupt(task.task_id):
            return {
                "interrupt_id": saved_interrupt.interrupt_id,
                "status": str(saved_interrupt.status),
                "node_id": saved_interrupt.node_id,
                "answer_payload": dict(selected_answer_payload),
                "action": "sheet_selection_required",
                "source_message_id": answer.source_message_id,
            }
        await self._schedule_execution(
            OrchestrationRequest(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                root_message_id=task.root_message_id,
                user_message=task.summary or "",
                requested_capability_id=task.requested_capability_id,
                metadata=resume_metadata,
            )
        )
        self._task_file_selection_resume_metadata.pop(task.task_id, None)
        return {
            "interrupt_id": saved_interrupt.interrupt_id,
            "status": str(saved_interrupt.status),
            "node_id": saved_interrupt.node_id,
            "answer_payload": dict(selected_answer_payload),
            "action": "resumed",
            "source_message_id": answer.source_message_id,
        }

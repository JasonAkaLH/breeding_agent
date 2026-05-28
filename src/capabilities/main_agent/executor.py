from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any, Mapping

from src.core.contracts import CapabilityContract, CapabilityExecutionError, CapabilityExecutionRequest, CapabilityExecutionResult, ExecutorPort
from src.core.enums import EventVisibility
from src.integrations.codex_skills import (
    SkillCatalog,
    SkillExecutionConfigError,
    SkillInputTextGenerator,
    SkillMatch,
    SkillScriptExecutionService,
    SkillScriptRunner,
    match_skills,
    normalize_skill_response_payload,
    resolve_skill_execution_config,
)
from src.integrations.codex_skills.missing_input_interrupt import build_missing_input_interrupt, missing_input_fields_from_payload
from src.integrations.llm_client import LLMClient, ReasoningEffort
from src.orchestration.answer_roles import (
    ANSWER_SCOPE_METADATA_KEY,
    RESPONSE_ROLE_METADATA_KEY,
    answer_scope_from_metadata,
    auto_skill_matching_enabled,
    response_role_from_metadata,
)

from .helpers import StreamGenerator, TransientEventPublisher, iter_stream_events, make_event, make_text_artifact
from .prompt_builder import build_artifact_context, build_dependency_context, build_main_agent_prompt
from .skill_output_artifacts import (
    SkillOutputArtifactManager,
)
from .workflow import MAIN_AGENT_CAPABILITY_DESCRIPTORS

_REASONING_EFFORTS: set[str] = {"minimal", "high", "max"}
_SENSITIVE_STREAM_METADATA_KEYS = {
    "api_key",
    "authorization",
    "auth",
    "token",
    "secret",
    "password",
    "prompt",
    "messages",
    "base_url",
    "url",
}


class MainAgentRespondCapability(CapabilityContract):
    capability_id = "main_agent.respond"
    version = "1"
    description = "生成主代理回答，并可兼容 Codex Skill 的提示词 / 脚本支持。"

    def __init__(
        self,
        *,
        stream_generator: StreamGenerator | None = None,
        stream_metadata: Mapping[str, Any] | None = None,
        default_reasoning_effort: ReasoningEffort = "minimal",
        skill_catalog: SkillCatalog | None = None,
        skill_catalog_resolver: Callable[[str | None], SkillCatalog] | None = None,
        script_runner: SkillScriptRunner | None = None,
        skill_input_text_generator: SkillInputTextGenerator | None = None,
        skill_output_artifact_manager: SkillOutputArtifactManager | None = None,
        transient_event_publisher: TransientEventPublisher | None = None,
        cancel_checker: Callable[[str], bool | Any] | None = None,
    ) -> None:
        self._stream_generator = stream_generator
        self._stream_metadata = self._sanitize_stream_metadata(stream_metadata or {})
        self._default_reasoning_effort = default_reasoning_effort
        self._skill_catalog = skill_catalog or SkillCatalog(())
        self._skill_catalog_resolver = skill_catalog_resolver
        self._script_runner = script_runner or SkillScriptRunner()
        self._skill_input_text_generator = skill_input_text_generator
        self._skill_output_artifact_manager = skill_output_artifact_manager
        self._transient_event_publisher = transient_event_publisher
        self._cancel_checker = cancel_checker
        self._script_execution_service = SkillScriptExecutionService(
            script_runner=self._script_runner,
            skill_input_text_generator=self._skill_input_text_generator,
        )

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        user_message = str(request.input_payload.get("user_message") or "")
        artifact_context = build_artifact_context(request.metadata)
        dependency_context = build_dependency_context(request.dependency_outputs)
        response_role = response_role_from_metadata(request.metadata)
        answer_scope = answer_scope_from_metadata(request.metadata)
        response_role_payload = self._response_role_payload(response_role=response_role, answer_scope=answer_scope)
        skill_matches, forced_skill_events = self._resolve_skill_matches(request, user_message)
        script_results, script_events, script_artifacts, missing_interrupt = await self._run_auto_scripts(
            request,
            user_message,
            artifact_context,
            skill_matches,
        )

        events = [*forced_skill_events, *script_events]
        for match in skill_matches:
            events.append(
                make_event(
                    request,
                    event_type="skill.matched",
                    payload={"skill_name": match.manifest.name, "score": match.score, "reason": match.reason},
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        if not skill_matches:
            events.append(
                make_event(
                    request,
                    event_type="skill.match_fallback",
                    payload={"reason": "no_skill_matched"},
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )

        if missing_interrupt is not None:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload={
                    "response_source": "skill_input_missing",
                    "matched_skills": [match.manifest.name for match in skill_matches],
                    "script_results": script_results,
                    "prompt_recorded": False,
                    **response_role_payload,
                },
                artifacts=tuple(script_artifacts),
                events=tuple(events),
                interrupt=missing_interrupt,
            )

        prompt = build_main_agent_prompt(
            user_message=user_message,
            skill_matches=skill_matches,
            artifact_context=artifact_context,
            script_results=script_results,
            dependency_context=dependency_context,
            memory_context=self._memory_context_from_metadata(request.metadata),
            response_role=response_role,
            answer_scope=answer_scope,
        )
        thinking_enabled = self._resolve_thinking_enabled(request.metadata)
        reasoning_effort = self._resolve_reasoning_effort(request.metadata, thinking_enabled=thinking_enabled)

        started_at = time.monotonic()
        chunks: list[str] = []
        stream_metadata: dict[str, Any] = dict(self._stream_metadata)
        model_edition = self._resolve_model_edition(request.metadata)
        answer_ordinal = 0
        reasoning_ordinal = 0
        answer_char_count = 0
        reasoning_char_count = 0
        try:
            stream_generator, stream_metadata = self._resolve_stream_binding(reasoning_effort=reasoning_effort)
            stream_metadata["reasoning_effort"] = reasoning_effort
            stream_metadata["thinking_enabled"] = thinking_enabled
            if model_edition:
                stream_metadata["model"] = model_edition
                stream_metadata["model_edition"] = model_edition
            async for stream_event in iter_stream_events(
                stream_generator,
                prompt,
                reasoning_effort=reasoning_effort,
                thinking=thinking_enabled,
                model_edition=model_edition,
            ):
                if await self._is_cancel_requested(request.task_id):
                    return self._cancelled_result(
                        request=request,
                        events=events,
                        stream_metadata=stream_metadata,
                        started_at=started_at,
                        matched_skills=skill_matches,
                        script_results=script_results,
                        script_artifacts=script_artifacts,
                        response_role_payload=response_role_payload,
                        answer_ordinal=answer_ordinal,
                        reasoning_ordinal=reasoning_ordinal,
                        answer_char_count=answer_char_count,
                        reasoning_char_count=reasoning_char_count,
                    )
                reasoning_delta = stream_event.get("reasoning")
                if thinking_enabled and reasoning_delta:
                    reasoning_ordinal += 1
                    reasoning_char_count += len(reasoning_delta)
                    reasoning_event = make_event(
                        request,
                        event_type="main_agent.reasoning_delta",
                        payload={"delta": reasoning_delta, "ordinal": reasoning_ordinal},
                        visibility=EventVisibility.FRONTEND,
                        ordinal=reasoning_ordinal,
                    )
                    await self._publish_transient(reasoning_event)

                answer_delta = stream_event.get("answer")
                if answer_delta:
                    if await self._is_cancel_requested(request.task_id):
                        return self._cancelled_result(
                            request=request,
                            events=events,
                            stream_metadata=stream_metadata,
                            started_at=started_at,
                            matched_skills=skill_matches,
                            script_results=script_results,
                            script_artifacts=script_artifacts,
                            response_role_payload=response_role_payload,
                            answer_ordinal=answer_ordinal,
                            reasoning_ordinal=reasoning_ordinal,
                            answer_char_count=answer_char_count,
                            reasoning_char_count=reasoning_char_count,
                        )
                    answer_ordinal += 1
                    answer_char_count += len(answer_delta)
                    chunks.append(answer_delta)
                    delta_event = make_event(
                        request,
                        event_type="main_agent.output_delta",
                        payload={
                            "delta": answer_delta,
                            "ordinal": answer_ordinal,
                            **response_role_payload,
                        },
                        visibility=EventVisibility.FRONTEND,
                        ordinal=answer_ordinal,
                    )
                    await self._publish_transient(delta_event)
        except Exception as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            diagnostic_payload = self._stream_diagnostic_payload(
                stream_metadata=stream_metadata,
                status="failed",
                stage="llm_stream",
                error_code="main_agent_llm_failed",
                error_type=exc.__class__.__name__,
                retriable=True,
                answer_chunk_count=answer_ordinal,
                reasoning_chunk_count=reasoning_ordinal,
                answer_char_count=answer_char_count,
                reasoning_char_count=reasoning_char_count,
                elapsed_ms=duration_ms,
            )
            events.append(
                make_event(
                    request,
                    event_type="main_agent.llm_stream_failed",
                    payload={
                        "prompt_recorded": False,
                        **diagnostic_payload,
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )

            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload={
                    "response_source": "llm",
                    "fallback_used": False,
                    "failure_reason": "provider_failed",
                    "stream_diagnostic": diagnostic_payload,
                    "matched_skills": [match.manifest.name for match in skill_matches],
                    "script_results": script_results,
                    "prompt_recorded": False,
                    **response_role_payload,
                },
                artifacts=tuple(script_artifacts),
                events=tuple(events),
                error=CapabilityExecutionError(
                    code="main_agent_llm_failed",
                    message="Main agent LLM call failed.",
                    retriable=True,
                    metadata={"prompt_recorded": False, **diagnostic_payload},
                ),
            )

        if await self._is_cancel_requested(request.task_id):
            return self._cancelled_result(
                request=request,
                events=events,
                stream_metadata=stream_metadata,
                started_at=started_at,
                matched_skills=skill_matches,
                script_results=script_results,
                script_artifacts=script_artifacts,
                response_role_payload=response_role_payload,
                answer_ordinal=answer_ordinal,
                reasoning_ordinal=reasoning_ordinal,
                answer_char_count=answer_char_count,
                reasoning_char_count=reasoning_char_count,
            )

        response_text = "".join(chunks)
        duration_ms = int((time.monotonic() - started_at) * 1000)
        final_event = make_event(
            request,
            event_type="main_agent.output_final",
            payload={
                "response_length": len(response_text),
                "answer_chunk_count": answer_ordinal,
                "reasoning_chunk_count": reasoning_ordinal,
                "answer_char_count": answer_char_count,
                "reasoning_char_count": reasoning_char_count,
                "duration_ms": duration_ms,
                **response_role_payload,
            },
            visibility=EventVisibility.FRONTEND,
        )
        events.append(final_event)
        events.append(
            make_event(
                request,
                event_type="main_agent.llm_call",
                payload={
                    **stream_metadata,
                    "status": "succeeded",
                    "prompt_recorded": False,
                    "duration_ms": duration_ms,
                    "matched_skill_count": len(skill_matches),
                    "uploaded_artifact_count": len(artifact_context),
                    "dependency_context_count": len(dependency_context),
                    **response_role_payload,
                },
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )
        artifact = make_text_artifact(
            task_id=request.task_id,
            node_id=request.node_id,
            text=response_text,
            response_role=response_role,
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload={
                "response_text": response_text,
                "response_source": "llm",
                "matched_skills": [match.manifest.name for match in skill_matches],
                "script_results": script_results,
                "prompt_recorded": False,
                **response_role_payload,
            },
            artifacts=(*script_artifacts, artifact),
            events=tuple(events),
        )

    def _resolve_skill_matches(self, request: CapabilityExecutionRequest, user_message: str) -> tuple[list[SkillMatch], list[Any]]:
        revision = self._skill_bundle_revision(request)
        try:
            skill_catalog = self._resolve_skill_catalog(revision)
        except KeyError:
            return [], [
                make_event(
                    request,
                    event_type="skill.bundle_missing",
                    payload={"skill_bundle_revision": revision},
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            ]
        forced_skill_name = self._forced_skill_name(request)
        if not forced_skill_name:
            if not auto_skill_matching_enabled(request.metadata):
                return [], [
                    make_event(
                        request,
                        event_type="skill.match_suppressed",
                        payload={"reason": "auto_skill_matching_disabled"},
                        visibility=EventVisibility.AUDIT_ONLY,
                    )
                ]
            return match_skills(user_message, skill_catalog), []

        manifest = skill_catalog.get(forced_skill_name)
        if manifest is None:
            return [], [
                make_event(
                    request,
                    event_type="skill.forced_missing",
                    payload={
                        "skill_name": forced_skill_name,
                        "capability_id": self._forced_skill_capability_id(request),
                        "source": self._forced_skill_source(request),
                        "skill_bundle_revision": revision,
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            ]

        return [SkillMatch(manifest=manifest, score=10_000, reason="forced_capability")], [
            make_event(
                request,
                event_type="skill.forced_selected",
                payload={
                    "skill_name": manifest.name,
                    "capability_id": self._forced_skill_capability_id(request),
                    "source": self._forced_skill_source(request),
                    "skill_bundle_revision": revision,
                },
                visibility=EventVisibility.AUDIT_ONLY,
            )
        ]

    @staticmethod
    def _response_role_payload(*, response_role: str | None, answer_scope: str | None) -> dict[str, str]:
        payload: dict[str, str] = {}
        if response_role:
            payload[RESPONSE_ROLE_METADATA_KEY] = response_role
        if answer_scope:
            payload[ANSWER_SCOPE_METADATA_KEY] = answer_scope
        return payload

    def _resolve_skill_catalog(self, revision: str | None) -> SkillCatalog:
        if self._skill_catalog_resolver is not None:
            return self._skill_catalog_resolver(revision)
        return self._skill_catalog

    @staticmethod
    def _forced_skill_name(request: CapabilityExecutionRequest) -> str:
        value = request.metadata.get("forced_skill_name")
        return str(value).strip() if value else ""

    @staticmethod
    def _forced_skill_capability_id(request: CapabilityExecutionRequest) -> str:
        value = request.metadata.get("forced_skill_capability_id")
        return str(value).strip() if value else ""

    @staticmethod
    def _forced_skill_source(request: CapabilityExecutionRequest) -> str:
        value = request.metadata.get("forced_skill_source")
        return str(value).strip() if value else "unknown"

    @staticmethod
    def _skill_bundle_revision(request: CapabilityExecutionRequest) -> str | None:
        value = request.metadata.get("skill_bundle_revision")
        return str(value).strip() if value else None

    async def _run_auto_scripts(self, request, user_message, artifact_context, skill_matches):
        script_results: list[dict[str, Any]] = []
        pending_file_artifacts = []
        events = []
        missing_interrupt = None
        raw_script_artifacts = request.metadata.get("skill_artifacts")
        script_input_artifacts = raw_script_artifacts if isinstance(raw_script_artifacts, list | tuple) else artifact_context
        for match in skill_matches:
            try:
                execution_config = resolve_skill_execution_config(match.manifest)
            except SkillExecutionConfigError:
                execution_config = None
            if execution_config is not None and execution_config.mode == "delegated_main_agent":
                continue
            for script in match.manifest.scripts:
                if not script.auto_run:
                    continue
                execution = await self._script_execution_service.execute(
                    manifest=match.manifest,
                    script=script,
                    user_message=user_message,
                    metadata=request.metadata,
                    artifact_context=tuple(artifact_context),
                    script_artifact_context=tuple(script_input_artifacts),
                    output_context={
                        "task_id": request.task_id,
                        "conversation_id": request.conversation_id,
                        "node_id": request.node_id,
                    },
                )
                resolution = execution.resolution
                if resolution is not None and resolution.diagnostics:
                    events.append(
                        make_event(
                            request,
                            event_type="skill.input_resolution_diagnostic",
                            payload={
                                "skill_name": match.manifest.name,
                                "entrypoint": script.name,
                                "diagnostics": list(resolution.diagnostics),
                            },
                            visibility=EventVisibility.AUDIT_ONLY,
                        )
                    )
                if resolution is not None and resolution.sources:
                    events.append(
                        make_event(
                            request,
                            event_type="skill.input_resolved",
                            payload=resolution.audit_payload(skill_name=match.manifest.name, entrypoint=script.name),
                            visibility=EventVisibility.AUDIT_ONLY,
                        )
                    )
                if execution.status == "missing_input":
                    self._record_missing_skill_input(
                        request=request,
                        script_results=script_results,
                        events=events,
                        skill_name=match.manifest.name,
                        entrypoint=script.name,
                        missing=execution.missing,
                        resolved_fields=resolution.resolved_fields if resolution is not None else (),
                    )
                    if missing_interrupt is None:
                        missing_interrupt = build_missing_input_interrupt(
                            request=request,
                            manifest=match.manifest,
                            skill_name=match.manifest.name,
                            entrypoint=script.name,
                            missing=execution.missing,
                        )
                    continue
                events.append(
                    make_event(
                        request,
                        event_type="skill.script_started",
                        payload={"skill_name": match.manifest.name, "entrypoint": script.name},
                        visibility=EventVisibility.AUDIT_ONLY,
                    )
                )
                if execution.status == "failed":
                    events.append(
                        make_event(
                            request,
                            event_type="skill.script_failed",
                            payload={"skill_name": match.manifest.name, "entrypoint": script.name, "reason": execution.failure_reason},
                            visibility=EventVisibility.AUDIT_ONLY,
                        )
                    )
                    continue
                output = normalize_skill_response_payload(execution.output)
                script_missing = missing_input_fields_from_payload(output)
                if script_missing:
                    script_results.append({"skill_name": match.manifest.name, "entrypoint": script.name, "output": output})
                    events.append(
                        make_event(
                            request,
                            event_type="skill.input_missing",
                            payload={
                                "skill_name": match.manifest.name,
                                "entrypoint": script.name,
                                "missing": list(script_missing),
                            },
                            visibility=EventVisibility.AUDIT_ONLY,
                        )
                    )
                    if missing_interrupt is None:
                        missing_interrupt = build_missing_input_interrupt(
                            request=request,
                            manifest=match.manifest,
                            skill_name=match.manifest.name,
                            entrypoint=script.name,
                            missing=script_missing,
                        )
                    continue
                artifact = execution.artifact
                if artifact is not None:
                    discard_diagnostics = self._discard_pending_skill_artifacts(pending_file_artifacts, script_results)
                    for diagnostic in discard_diagnostics:
                        events.append(
                            make_event(
                                request,
                                event_type="skill.output_file_rejected",
                                payload={
                                    "skill_name": match.manifest.name,
                                    "entrypoint": script.name,
                                    "path": diagnostic.get("path", ""),
                                    "reason": diagnostic.get("reason", "pending_artifact_cleanup_failed"),
                                },
                                visibility=EventVisibility.AUDIT_ONLY,
                            )
                        )
                    pending_file_artifacts.append(artifact)
                rejections = execution.rejections
                if rejections:
                    for rejection in rejections:
                        events.append(
                            make_event(
                                request,
                                event_type="skill.output_file_rejected",
                                payload={
                                    "skill_name": match.manifest.name,
                                    "entrypoint": script.name,
                                    "path": rejection.path,
                                    "reason": rejection.reason,
                                },
                                visibility=EventVisibility.AUDIT_ONLY,
                            )
                        )
                if execution.output_file_count:
                    events.append(
                        make_event(
                            request,
                            event_type="skill.output_file_collected",
                            payload={
                                "skill_name": match.manifest.name,
                                "entrypoint": script.name,
                                "file_count": execution.output_file_count,
                            },
                            visibility=EventVisibility.AUDIT_ONLY,
                        )
                    )
                script_results.append({"skill_name": match.manifest.name, "entrypoint": script.name, "output": output})
                events.append(
                    make_event(
                        request,
                        event_type="skill.script_completed",
                        payload={"skill_name": match.manifest.name, "entrypoint": script.name, "schema_validated": True},
                        visibility=EventVisibility.AUDIT_ONLY,
                    )
                )
        return script_results, tuple(events), tuple(pending_file_artifacts), missing_interrupt

    def _discard_pending_skill_artifacts(self, script_artifacts: list, script_results: list[dict[str, Any]]) -> tuple[dict[str, str], ...]:
        cleanup_diagnostics: list[dict[str, str]] = []
        if self._skill_output_artifact_manager is None:
            script_artifacts.clear()
            return ()
        for existing in list(script_artifacts):
            rejection = self._skill_output_artifact_manager.discard_unsaved_artifact(existing)
            if rejection is not None:
                cleanup_diagnostics.append({"path": rejection.path, "reason": rejection.reason, "message": rejection.message})
        script_artifacts.clear()
        for result in script_results:
            output = result.get("output")
            if not isinstance(output, dict) or "output_files" not in output:
                continue
            output.pop("output_files", None)
            output_diagnostics = output.setdefault("output_file_diagnostics", [])
            if isinstance(output_diagnostics, list):
                output_diagnostics.append(
                    {
                        "path": "",
                        "reason": "superseded_by_later_skill_output",
                        "message": "A later Skill output file replaced this output in the same response.",
                    }
                )
                output_diagnostics.extend(cleanup_diagnostics)
        return tuple(cleanup_diagnostics)

    def _resolve_stream_binding(self, *, reasoning_effort: ReasoningEffort) -> tuple[StreamGenerator, dict[str, Any]]:
        if self._stream_generator is not None:
            return self._stream_generator, dict(self._stream_metadata)
        client = LLMClient()
        metadata = client.safe_metadata(config_source="environment", reasoning_effort=reasoning_effort)
        stream_generator = getattr(client, "generate_text_with_thinking", None)
        if not callable(stream_generator):
            stream_generator = client.stream_text
        return stream_generator, self._sanitize_stream_metadata(metadata)

    def _resolve_reasoning_effort(self, metadata: Mapping[str, Any], *, thinking_enabled: bool) -> ReasoningEffort:
        if not thinking_enabled:
            return "minimal"
        explicit = metadata.get("main_agent_reasoning_effort")
        if isinstance(explicit, str) and explicit in _REASONING_EFFORTS:
            return explicit  # type: ignore[return-value]
        return self._default_reasoning_effort

    def _resolve_thinking_enabled(self, metadata: Mapping[str, Any]) -> bool:
        if "main_agent_thinking_enabled" in metadata:
            return self._is_truthy(metadata.get("main_agent_thinking_enabled"))
        return self._is_truthy(metadata.get("deep_thinking"))

    @staticmethod
    def _resolve_model_edition(metadata: Mapping[str, Any]) -> str | None:
        value = metadata.get("model_edition")
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        return None

    @staticmethod
    def _is_truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
        return bool(value)

    @staticmethod
    def _memory_context_from_metadata(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
        value = metadata.get("conversation_memory") or metadata.get("memory_context") or {}
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _missing_skill_input_output(missing: tuple[str, ...]) -> dict[str, Any]:
        missing_list = list(missing)
        fields = "、".join(missing_list)
        return {
            "ok": False,
            "error": {
                "type": "missing_input",
                "message": f"缺少 Skill 脚本必需参数：{fields}。",
            },
            "missing": missing_list,
            "answer": f"缺少 Skill 脚本必需参数：{fields}。请补充后再执行。",
        }

    @classmethod
    def _record_missing_skill_input(
        cls,
        *,
        request: CapabilityExecutionRequest,
        script_results: list[dict[str, Any]],
        events: list,
        skill_name: str,
        entrypoint: str,
        missing: tuple[str, ...],
        resolved_fields: tuple[str, ...] = (),
    ) -> None:
        output = cls._missing_skill_input_output(missing)
        script_results.append(
            {
                "skill_name": skill_name,
                "entrypoint": entrypoint,
                "output": output,
                "input_resolution": {
                    "resolved_fields": list(resolved_fields),
                    "missing": list(missing),
                },
            }
        )
        events.append(
            make_event(
                request,
                event_type="skill.input_missing",
                payload={
                    "skill_name": skill_name,
                    "entrypoint": entrypoint,
                    "missing": list(missing),
                },
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )

    @staticmethod
    def _sanitize_stream_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
        return {
            str(key): value
            for key, value in metadata.items()
            if str(key).lower() not in _SENSITIVE_STREAM_METADATA_KEYS
        }

    async def _publish_transient(self, event) -> None:
        if self._transient_event_publisher is None:
            return
        await self._transient_event_publisher(event)

    async def _is_cancel_requested(self, task_id: str) -> bool:
        if self._cancel_checker is None:
            return False
        result = self._cancel_checker(task_id)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    @staticmethod
    def _stream_diagnostic_payload(
        *,
        stream_metadata: Mapping[str, Any],
        status: str,
        stage: str,
        error_code: str,
        error_type: str,
        retriable: bool,
        answer_chunk_count: int,
        reasoning_chunk_count: int,
        answer_char_count: int,
        reasoning_char_count: int,
        elapsed_ms: int,
        cancel_source: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            **dict(stream_metadata),
            "status": status,
            "stage": stage,
            "error_code": error_code,
            "error_type": error_type,
            "retriable": retriable,
            "partial_output_discarded": True,
            "answer_chunk_count": answer_chunk_count,
            "reasoning_chunk_count": reasoning_chunk_count,
            "answer_char_count": answer_char_count,
            "reasoning_char_count": reasoning_char_count,
            "elapsed_ms": elapsed_ms,
        }
        if cancel_source is not None:
            payload["cancel_source"] = cancel_source
        return payload

    def _cancelled_result(
        self,
        *,
        request: CapabilityExecutionRequest,
        events: list,
        stream_metadata: Mapping[str, Any],
        started_at: float,
        matched_skills: list[SkillMatch],
        script_results: list[dict[str, Any]],
        script_artifacts: tuple,
        response_role_payload: Mapping[str, Any],
        answer_ordinal: int,
        reasoning_ordinal: int,
        answer_char_count: int,
        reasoning_char_count: int,
    ) -> CapabilityExecutionResult:
        diagnostic_payload = self._stream_diagnostic_payload(
            stream_metadata=stream_metadata,
            status="cancelled",
            stage="llm_stream",
            error_code="main_agent_stream_cancelled",
            error_type="CancelledByUser",
            retriable=False,
            answer_chunk_count=answer_ordinal,
            reasoning_chunk_count=reasoning_ordinal,
            answer_char_count=answer_char_count,
            reasoning_char_count=reasoning_char_count,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            cancel_source="user",
        )
        events.append(
            make_event(
                request,
                event_type="main_agent.stream_cancelled",
                payload={"prompt_recorded": False, **diagnostic_payload},
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload={
                "response_source": "llm",
                "fallback_used": False,
                "stream_diagnostic": diagnostic_payload,
                "matched_skills": [match.manifest.name for match in matched_skills],
                "script_results": script_results,
                "prompt_recorded": False,
                **dict(response_role_payload),
            },
            artifacts=tuple(script_artifacts),
            events=tuple(events),
            error=CapabilityExecutionError(
                code="main_agent_stream_cancelled",
                message="Main agent stream was cancelled before completion.",
                retriable=False,
                metadata={"prompt_recorded": False, **diagnostic_payload},
            ),
        )


class MainAgentExecutor(ExecutorPort):
    def __init__(
        self,
        *,
        stream_generator: StreamGenerator | None = None,
        stream_metadata: Mapping[str, Any] | None = None,
        default_reasoning_effort: ReasoningEffort = "minimal",
        skill_catalog: SkillCatalog | None = None,
        skill_catalog_resolver: Callable[[str | None], SkillCatalog] | None = None,
        script_runner: SkillScriptRunner | None = None,
        skill_input_text_generator: SkillInputTextGenerator | None = None,
        skill_output_artifact_manager: SkillOutputArtifactManager | None = None,
        transient_event_publisher: TransientEventPublisher | None = None,
        cancel_checker: Callable[[str], bool | Any] | None = None,
    ) -> None:
        self._capabilities: dict[str, CapabilityContract] = {
            "main_agent.respond": MainAgentRespondCapability(
                stream_generator=stream_generator,
                stream_metadata=stream_metadata,
                default_reasoning_effort=default_reasoning_effort,
                skill_catalog=skill_catalog,
                skill_catalog_resolver=skill_catalog_resolver,
                script_runner=script_runner,
                skill_input_text_generator=skill_input_text_generator,
                skill_output_artifact_manager=skill_output_artifact_manager,
                transient_event_publisher=transient_event_publisher,
                cancel_checker=cancel_checker,
            )
        }

    def supports(self, capability_id: str) -> bool:
        return capability_id in self._capabilities

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        capability = self._capabilities.get(request.capability_id)
        if capability is None:
            raise ValueError(f"Unsupported main agent capability_id: {request.capability_id}")
        return await capability.execute(request)

    @property
    def supported_capabilities(self) -> tuple[str, ...]:
        return tuple(descriptor.capability_id for descriptor in MAIN_AGENT_CAPABILITY_DESCRIPTORS)

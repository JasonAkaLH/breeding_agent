from __future__ import annotations

import time
from typing import Any, Mapping

from src.core.contracts import CapabilityContract, CapabilityExecutionError, CapabilityExecutionRequest, CapabilityExecutionResult, ExecutorPort
from src.core.enums import EventVisibility
from src.integrations.codex_skills import (
    SkillCatalog,
    SkillInputResolutionContext,
    SkillInputTextGenerator,
    SkillScriptError,
    SkillScriptRunner,
    match_skills,
    resolve_skill_inputs_with_llm,
)
from src.integrations.llm_client import LLMClient, ReasoningEffort

from .helpers import LiveEventRecorder, StreamGenerator, iter_stream_events, make_event, make_text_artifact
from .prompt_builder import build_artifact_context, build_dependency_context, build_main_agent_prompt
from .skill_output_artifacts import (
    SKILL_OUTPUT_ARTIFACT_INTERNAL_KEY,
    SKILL_OUTPUT_REJECTIONS_INTERNAL_KEY,
    SkillOutputArtifactManager,
)
from .workflow import MAIN_AGENT_CAPABILITY_DESCRIPTORS

_REASONING_EFFORTS: set[str] = {"minimal", "low", "medium", "high"}
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
        script_runner: SkillScriptRunner | None = None,
        skill_input_text_generator: SkillInputTextGenerator | None = None,
        skill_output_artifact_manager: SkillOutputArtifactManager | None = None,
        live_event_recorder: LiveEventRecorder | None = None,
    ) -> None:
        self._stream_generator = stream_generator
        self._stream_metadata = self._sanitize_stream_metadata(stream_metadata or {})
        self._default_reasoning_effort = default_reasoning_effort
        self._skill_catalog = skill_catalog or SkillCatalog(())
        self._script_runner = script_runner or SkillScriptRunner()
        self._skill_input_text_generator = skill_input_text_generator
        self._skill_output_artifact_manager = skill_output_artifact_manager
        self._live_event_recorder = live_event_recorder

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        user_message = str(request.input_payload.get("user_message") or "")
        artifact_context = build_artifact_context(request.metadata)
        dependency_context = build_dependency_context(request.dependency_outputs)
        skill_matches = match_skills(user_message, self._skill_catalog)
        script_results, script_events, script_artifacts = await self._run_auto_scripts(request, user_message, artifact_context, skill_matches)
        prompt = build_main_agent_prompt(
            user_message=user_message,
            skill_matches=skill_matches,
            artifact_context=artifact_context,
            script_results=script_results,
            dependency_context=dependency_context,
            memory_context=self._memory_context_from_metadata(request.metadata),
        )

        events = list(script_events)
        reasoning_effort = self._resolve_reasoning_effort(request.metadata)
        thinking_enabled = self._resolve_thinking_enabled(request.metadata)
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

        started_at = time.monotonic()
        chunks: list[str] = []
        stream_metadata: dict[str, Any] = dict(self._stream_metadata)
        try:
            stream_generator, stream_metadata = self._resolve_stream_binding(reasoning_effort=reasoning_effort)
            stream_metadata["reasoning_effort"] = reasoning_effort
            stream_metadata["thinking_enabled"] = thinking_enabled
            answer_ordinal = 0
            reasoning_ordinal = 0
            async for stream_event in iter_stream_events(
                stream_generator,
                prompt,
                reasoning_effort=reasoning_effort,
                thinking=thinking_enabled,
            ):
                reasoning_delta = stream_event.get("reasoning")
                if thinking_enabled and reasoning_delta:
                    reasoning_ordinal += 1
                    reasoning_event = make_event(
                        request,
                        event_type="main_agent.reasoning_delta",
                        payload={"delta": reasoning_delta, "ordinal": reasoning_ordinal},
                        visibility=EventVisibility.FRONTEND,
                        ordinal=reasoning_ordinal,
                    )
                    await self._record_or_collect(reasoning_event, events)

                answer_delta = stream_event.get("answer")
                if answer_delta:
                    answer_ordinal += 1
                    chunks.append(answer_delta)
                    delta_event = make_event(
                        request,
                        event_type="main_agent.output_delta",
                        payload={"delta": answer_delta, "ordinal": answer_ordinal},
                        visibility=EventVisibility.FRONTEND,
                        ordinal=answer_ordinal,
                    )
                    await self._record_or_collect(delta_event, events)
        except Exception as exc:
            events.append(
                make_event(
                    request,
                    event_type="main_agent.llm_fallback",
                    payload={
                        **stream_metadata,
                        "fallback_reason": "provider_failed",
                        "prompt_recorded": False,
                        "diagnostic": self._safe_exception_diagnostic(exc),
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
                    "fallback_used": True,
                    "fallback_reason": "provider_failed",
                    "matched_skills": [match.manifest.name for match in skill_matches],
                    "script_results": script_results,
                    "prompt_recorded": False,
                },
                artifacts=tuple(script_artifacts),
                events=tuple(events),
                error=CapabilityExecutionError(
                    code="main_agent_llm_failed",
                    message="Main agent LLM call failed.",
                    retriable=True,
                    metadata={"prompt_recorded": False},
                ),
            )

        response_text = "".join(chunks)
        duration_ms = int((time.monotonic() - started_at) * 1000)
        final_event = make_event(
            request,
            event_type="main_agent.output_final",
            payload={"response_length": len(response_text)},
            visibility=EventVisibility.FRONTEND,
        )
        await self._record_or_collect(final_event, events)
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
                },
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )
        artifact = make_text_artifact(task_id=request.task_id, node_id=request.node_id, text=response_text)
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
            },
            artifacts=(*script_artifacts, artifact),
            events=tuple(events),
        )

    async def _run_auto_scripts(self, request, user_message, artifact_context, skill_matches):
        script_results: list[dict[str, Any]] = []
        pending_file_artifacts = []
        events = []
        raw_script_artifacts = request.metadata.get("skill_artifacts")
        script_input_artifacts = raw_script_artifacts if isinstance(raw_script_artifacts, list | tuple) else artifact_context
        for match in skill_matches:
            for script in match.manifest.scripts:
                if not script.auto_run:
                    continue
                base_payload = {
                    "query": user_message,
                    "uploaded_artifacts": list(script_input_artifacts),
                    "metadata": self._script_safe_metadata(request.metadata),
                }
                resolution = await resolve_skill_inputs_with_llm(
                    match.manifest,
                    script,
                    base_payload,
                    SkillInputResolutionContext.from_metadata(
                        query=user_message,
                        metadata=request.metadata,
                        artifact_summaries=tuple(artifact_context),
                    ),
                    text_generator=self._skill_input_text_generator,
                )
                if resolution.diagnostics:
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
                if resolution.sources:
                    events.append(
                        make_event(
                            request,
                            event_type="skill.input_resolved",
                            payload=resolution.audit_payload(skill_name=match.manifest.name, entrypoint=script.name),
                            visibility=EventVisibility.AUDIT_ONLY,
                        )
                    )
                if resolution.missing:
                    self._record_missing_skill_input(
                        request=request,
                        script_results=script_results,
                        events=events,
                        skill_name=match.manifest.name,
                        entrypoint=script.name,
                        missing=resolution.missing,
                        resolved_fields=resolution.resolved_fields,
                    )
                    continue
                contract_missing = self._missing_script_contract_inputs(script.input_contract.required, resolution.payload)
                if contract_missing:
                    self._record_missing_skill_input(
                        request=request,
                        script_results=script_results,
                        events=events,
                        skill_name=match.manifest.name,
                        entrypoint=script.name,
                        missing=contract_missing,
                        resolved_fields=resolution.resolved_fields,
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
                try:
                    output = await self._script_runner.run(
                        match.manifest,
                        script,
                        resolution.payload,
                        output_context={
                            "task_id": request.task_id,
                            "conversation_id": request.conversation_id,
                            "node_id": request.node_id,
                        },
                    )
                except SkillScriptError as exc:
                    events.append(
                        make_event(
                            request,
                            event_type="skill.script_failed",
                            payload={"skill_name": match.manifest.name, "entrypoint": script.name, "reason": str(exc)[:200]},
                            visibility=EventVisibility.AUDIT_ONLY,
                        )
                    )
                    continue
                artifact = output.pop(SKILL_OUTPUT_ARTIFACT_INTERNAL_KEY, None)
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
                rejections = output.pop(SKILL_OUTPUT_REJECTIONS_INTERNAL_KEY, ())
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
                if "output_files" in output:
                    events.append(
                        make_event(
                            request,
                            event_type="skill.output_file_collected",
                            payload={
                                "skill_name": match.manifest.name,
                                "entrypoint": script.name,
                                "file_count": len(output.get("output_files") or ()),
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
        return script_results, tuple(events), tuple(pending_file_artifacts)

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

    def _resolve_reasoning_effort(self, metadata: Mapping[str, Any]) -> ReasoningEffort:
        explicit = metadata.get("main_agent_reasoning_effort")
        if isinstance(explicit, str) and explicit in _REASONING_EFFORTS:
            return explicit  # type: ignore[return-value]
        return self._default_reasoning_effort

    def _resolve_thinking_enabled(self, metadata: Mapping[str, Any]) -> bool:
        if "main_agent_thinking_enabled" in metadata:
            return self._is_truthy(metadata.get("main_agent_thinking_enabled"))
        return self._is_truthy(metadata.get("deep_thinking"))

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

    @classmethod
    def _script_safe_metadata(cls, metadata: Mapping[str, Any]) -> dict[str, Any]:
        blocked = {"conversation_memory", "memory_context", "recent_messages", "history_summary", "resolved_user_message"}
        return {
            str(key): value
            for key, value in metadata.items()
            if str(key).lower() not in blocked
        }

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
    def _missing_script_contract_inputs(required: tuple[str, ...], payload: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(key for key in required if key not in payload)

    @staticmethod
    def _sanitize_stream_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
        return {
            str(key): value
            for key, value in metadata.items()
            if str(key).lower() not in _SENSITIVE_STREAM_METADATA_KEYS
        }

    @staticmethod
    def _safe_exception_diagnostic(exc: Exception) -> str:
        return exc.__class__.__name__

    async def _record_or_collect(self, event, events: list) -> None:
        if self._live_event_recorder is None:
            events.append(event)
            return
        await self._live_event_recorder(event)


class MainAgentExecutor(ExecutorPort):
    def __init__(
        self,
        *,
        stream_generator: StreamGenerator | None = None,
        stream_metadata: Mapping[str, Any] | None = None,
        default_reasoning_effort: ReasoningEffort = "minimal",
        skill_catalog: SkillCatalog | None = None,
        script_runner: SkillScriptRunner | None = None,
        skill_input_text_generator: SkillInputTextGenerator | None = None,
        skill_output_artifact_manager: SkillOutputArtifactManager | None = None,
        live_event_recorder: LiveEventRecorder | None = None,
    ) -> None:
        self._capabilities: dict[str, CapabilityContract] = {
            "main_agent.respond": MainAgentRespondCapability(
                stream_generator=stream_generator,
                stream_metadata=stream_metadata,
                default_reasoning_effort=default_reasoning_effort,
                skill_catalog=skill_catalog,
                script_runner=script_runner,
                skill_input_text_generator=skill_input_text_generator,
                skill_output_artifact_manager=skill_output_artifact_manager,
                live_event_recorder=live_event_recorder,
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

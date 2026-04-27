from __future__ import annotations

import time
from typing import Any, Mapping

from src.core.contracts import CapabilityContract, CapabilityExecutionError, CapabilityExecutionRequest, CapabilityExecutionResult, ExecutorPort
from src.core.enums import EventVisibility
from src.integrations.codex_skills import SkillCatalog, SkillScriptError, SkillScriptRunner, match_skills
from src.integrations.llm_client import LLMClient

from .helpers import LiveEventRecorder, StreamGenerator, iter_stream, make_event, make_text_artifact
from .prompt_builder import build_artifact_context, build_main_agent_prompt
from .workflow import MAIN_AGENT_CAPABILITY_DESCRIPTORS

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
    description = "Generate a main-agent response with optional Codex Skill-compatible prompt/script support."

    def __init__(
        self,
        *,
        stream_generator: StreamGenerator | None = None,
        stream_metadata: Mapping[str, Any] | None = None,
        skill_catalog: SkillCatalog | None = None,
        script_runner: SkillScriptRunner | None = None,
        live_event_recorder: LiveEventRecorder | None = None,
    ) -> None:
        self._stream_generator = stream_generator
        self._stream_metadata = self._sanitize_stream_metadata(stream_metadata or {})
        self._skill_catalog = skill_catalog or SkillCatalog(())
        self._script_runner = script_runner or SkillScriptRunner()
        self._live_event_recorder = live_event_recorder

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        user_message = str(request.input_payload.get("user_message") or "")
        artifact_context = build_artifact_context(request.metadata)
        skill_matches = match_skills(user_message, self._skill_catalog)
        script_results, script_events = await self._run_auto_scripts(request, user_message, artifact_context, skill_matches)
        prompt = build_main_agent_prompt(
            user_message=user_message,
            skill_matches=skill_matches,
            artifact_context=artifact_context,
            script_results=script_results,
        )

        events = list(script_events)
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
            stream_generator, stream_metadata = self._resolve_stream_binding()
            ordinal = 0
            async for chunk in iter_stream(stream_generator, prompt):
                ordinal += 1
                chunks.append(chunk)
                delta_event = make_event(
                    request,
                    event_type="main_agent.output_delta",
                    payload={"delta": chunk, "ordinal": ordinal},
                    visibility=EventVisibility.FRONTEND,
                    ordinal=ordinal,
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
                output_payload={"response_source": "llm", "fallback_used": True, "fallback_reason": "provider_failed"},
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
            artifacts=(artifact,),
            events=tuple(events),
        )

    async def _run_auto_scripts(self, request, user_message, artifact_context, skill_matches):
        script_results: list[dict[str, Any]] = []
        events = []
        for match in skill_matches:
            for script in match.manifest.scripts:
                if not script.auto_run:
                    continue
                events.append(
                    make_event(
                        request,
                        event_type="skill.script_started",
                        payload={"skill_name": match.manifest.name, "entrypoint": script.name},
                        visibility=EventVisibility.AUDIT_ONLY,
                    )
                )
                payload = {"query": user_message, "uploaded_artifacts": artifact_context, "metadata": dict(request.metadata)}
                try:
                    output = await self._script_runner.run(match.manifest, script, payload)
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
                script_results.append({"skill_name": match.manifest.name, "entrypoint": script.name, "output": output})
                events.append(
                    make_event(
                        request,
                        event_type="skill.script_completed",
                        payload={"skill_name": match.manifest.name, "entrypoint": script.name, "schema_validated": True},
                        visibility=EventVisibility.AUDIT_ONLY,
                    )
                )
        return script_results, tuple(events)

    def _resolve_stream_binding(self) -> tuple[StreamGenerator, dict[str, Any]]:
        if self._stream_generator is not None:
            return self._stream_generator, dict(self._stream_metadata)
        client = LLMClient()
        metadata = client.safe_metadata(config_source="default_config_path", reasoning_effort="minimal")
        return client.stream_text, self._sanitize_stream_metadata(metadata)

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
        skill_catalog: SkillCatalog | None = None,
        script_runner: SkillScriptRunner | None = None,
        live_event_recorder: LiveEventRecorder | None = None,
    ) -> None:
        self._capabilities: dict[str, CapabilityContract] = {
            "main_agent.respond": MainAgentRespondCapability(
                stream_generator=stream_generator,
                stream_metadata=stream_metadata,
                skill_catalog=skill_catalog,
                script_runner=script_runner,
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

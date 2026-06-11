from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.core.contracts import CapabilityExecutionError, CapabilityExecutionRequest, CapabilityExecutionResult, ExecutorPort
from src.core.enums import ArtifactType, EventVisibility
from src.core.models import Artifact, EventRecord
from src.integrations.agent_skills import (
    SkillExecutionConfig,
    SkillExecutionConfigError,
    SkillManifest,
    SkillPlatformExecutionContext,
    SkillPlatformHandlerRegistry,
    SkillScriptExecutionService,
    SkillScriptRunner,
    SkillRuntimeState,
    SkillServiceRegistry,
    build_skill_artifact_context,
    build_skill_script_artifact_context,
    build_skill_safe_metadata,
    call_platform_handler,
    coerce_skill_response_text,
    normalize_skill_response_payload,
    resolve_skill_execution_config,
    select_skill_entrypoint,
)
from src.integrations.agent_skills.missing_input_interrupt import (
    build_missing_input_interrupt_with_question,
    missing_input_fields_from_payload,
)


@dataclass(slots=True, frozen=True)
class _ResolvedSkill:
    revision: str | None
    manifest: SkillManifest
    capability_id: str
    public_skill_roots: tuple[Any, ...] = ()


class _UnknownSkillBundleRevisionError(KeyError):
    pass


class SkillExecutor(ExecutorPort):
    def __init__(
        self,
        *,
        runtime_state: SkillRuntimeState,
        script_runner: SkillScriptRunner | None = None,
        skill_input_text_generator=None,
        platform_handler_registry: SkillPlatformHandlerRegistry | None = None,
        service_registry: SkillServiceRegistry | None = None,
    ) -> None:
        self._runtime_state = runtime_state
        self._skill_input_text_generator = skill_input_text_generator
        self._script_service = SkillScriptExecutionService(
            script_runner=script_runner or SkillScriptRunner(),
            skill_input_text_generator=skill_input_text_generator,
        )
        self._platform_handler_registry = platform_handler_registry or SkillPlatformHandlerRegistry(
            public_skill_roots=runtime_state.active_bundle.public_skill_roots,
        )
        self._service_registry = service_registry or SkillServiceRegistry()

    def supports(self, capability_id: str) -> bool:
        try:
            return capability_id in set(self._runtime_state.known_skill_capability_ids())
        except Exception:
            return False

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        try:
            resolved = self._resolve_skill(request)
        except _UnknownSkillBundleRevisionError:
            return self._error_result(
                request,
                code="skill_bundle_revision_missing",
                message="Skill bundle revision is not available.",
            )
        except KeyError:
            return self._error_result(request, code="skill_capability_not_registered", message="Skill capability is not registered.")
        except SkillExecutionConfigError as exc:
            return self._error_result(request, code="skill_execution_config_invalid", message=str(exc))

        try:
            execution = resolve_skill_execution_config(resolved.manifest)
        except SkillExecutionConfigError as exc:
            return self._error_result(request, code="skill_execution_config_invalid", message=str(exc))

        started_event = self._make_event(
            request,
            event_type="skill.execution_started",
            payload={
                "capability_id": request.capability_id,
                "skill_name": resolved.manifest.name,
                "skill_bundle_revision": resolved.revision,
                "mode": execution.mode,
                "answer_mode": execution.answer_mode,
            },
            visibility=EventVisibility.AUDIT_ONLY,
        )
        events: list[EventRecord] = [started_event]
        artifact_context = build_skill_artifact_context(request.metadata)
        user_message = self._resolve_user_message(request)
        started_at = time.monotonic()

        if execution.mode == "delegated_main_agent":
            failed = self._make_event(
                request,
                event_type="skill.execution_failed",
                payload={
                    "capability_id": request.capability_id,
                    "skill_name": resolved.manifest.name,
                    "mode": execution.mode,
                    "reason": "delegated_main_agent_not_executable",
                },
                visibility=EventVisibility.AUDIT_ONLY,
            )
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload={"skill_name": resolved.manifest.name},
                events=(started_event, failed),
                error=CapabilityExecutionError(
                    code="skill_execution_mode_not_supported",
                    message="Delegated main-agent skills are not executed by SkillExecutor.",
                    retriable=False,
                ),
            )

        if execution.mode == "python_subprocess":
            if execution.services:
                failed = self._make_event(
                    request,
                    event_type="skill.execution_failed",
                    payload={
                        "capability_id": request.capability_id,
                        "skill_name": resolved.manifest.name,
                        "mode": execution.mode,
                        "reason": "service_binding_not_allowed_for_python_subprocess",
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
                return CapabilityExecutionResult(
                    capability_id=request.capability_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    output_payload={"skill_name": resolved.manifest.name},
                    events=(started_event, failed),
                    error=CapabilityExecutionError(
                        code="skill_service_denied",
                        message="python_subprocess skills cannot bind controlled services.",
                        retriable=False,
                    ),
                )
            try:
                script = select_skill_entrypoint(resolved.manifest)
            except SkillExecutionConfigError as exc:
                failed = self._make_event(
                    request,
                    event_type="skill.execution_failed",
                    payload={
                        "capability_id": request.capability_id,
                        "skill_name": resolved.manifest.name,
                        "mode": execution.mode,
                        "reason": "entrypoint_not_allowed",
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
                return CapabilityExecutionResult(
                    capability_id=request.capability_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    output_payload={"skill_name": resolved.manifest.name},
                    events=(started_event, failed),
                    error=CapabilityExecutionError(
                        code="skill_entrypoint_not_allowed",
                        message=str(exc),
                        retriable=False,
                    ),
                )
            script_artifact_context = build_skill_script_artifact_context(
                request.metadata,
                fallback_artifact_context=artifact_context,
            )
            return await self._execute_script_skill(
                request=request,
                resolved=resolved,
                execution=execution,
                user_message=user_message,
                script_artifact_context=script_artifact_context,
                script=script,
                started_at=started_at,
                prior_events=events,
            )

        return await self._execute_platform_service_skill(
            request=request,
            resolved=resolved,
            execution=execution,
            user_message=user_message,
            artifact_context=artifact_context,
            started_at=started_at,
            prior_events=events,
        )

    def _resolve_skill(self, request: CapabilityExecutionRequest) -> _ResolvedSkill:
        revision = str(request.metadata.get("skill_bundle_revision") or "").strip() or None
        try:
            bundle = self._runtime_state.bundle_for_revision(revision)
        except KeyError as exc:
            raise _UnknownSkillBundleRevisionError(revision or "") from exc
        skill_name = bundle.skill_capabilities.skill_name_by_capability_id.get(request.capability_id)
        if not skill_name:
            raise KeyError(request.capability_id)
        manifest = bundle.catalog.get(skill_name)
        if manifest is None:
            raise SkillExecutionConfigError(f"Missing manifest for {request.capability_id}")
        return _ResolvedSkill(
            revision=revision,
            manifest=manifest,
            capability_id=request.capability_id,
            public_skill_roots=bundle.public_skill_roots,
        )

    async def _execute_script_skill(
        self,
        *,
        request: CapabilityExecutionRequest,
        resolved: _ResolvedSkill,
        execution,
        user_message: str,
        script_artifact_context: tuple[Mapping[str, Any], ...],
        script,
        started_at: float,
        prior_events: list[EventRecord],
    ) -> CapabilityExecutionResult:
        script_result = await self._script_service.execute(
            manifest=resolved.manifest,
            script=script,
            user_message=user_message,
            metadata=request.metadata,
            artifact_context=build_skill_artifact_context(request.metadata),
            script_artifact_context=script_artifact_context,
            output_context={
                "task_id": request.task_id,
                "conversation_id": request.conversation_id,
                "node_id": request.node_id,
            },
        )
        events = list(prior_events)
        resolution_prompt_profile = (
            getattr(script_result.resolution, "prompt_profile", None)
            if script_result.resolution is not None
            else None
        )
        if isinstance(resolution_prompt_profile, Mapping):
            events.append(
                self._make_event(
                    request,
                    event_type="skill.input_resolution_prompt_profile",
                    payload={
                        "skill_name": resolved.manifest.name,
                        "entrypoint": script.name,
                        "prompt_profile": dict(resolution_prompt_profile),
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        if script_result.resolution is not None and script_result.resolution.diagnostics:
            events.append(
                self._make_event(
                    request,
                    event_type="skill.input_resolution_diagnostic",
                    payload={
                        "skill_name": resolved.manifest.name,
                        "entrypoint": script.name,
                        "diagnostics": list(script_result.resolution.diagnostics),
                        **(
                            {"prompt_profile": dict(resolution_prompt_profile)}
                            if isinstance(resolution_prompt_profile, Mapping)
                            else {}
                        ),
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        if script_result.resolution is not None and script_result.resolution.sources:
            events.append(
                self._make_event(
                    request,
                    event_type="skill.input_resolved",
                    payload=script_result.resolution.audit_payload(skill_name=resolved.manifest.name, entrypoint=script.name),
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        if script_result.status == "missing_input":
            events.append(
                self._make_event(
                    request,
                    event_type="skill.input_missing",
                    payload={
                        "skill_name": resolved.manifest.name,
                        "entrypoint": script.name,
                        "missing": list(script_result.missing),
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
            events.append(
                self._make_event(
                    request,
                    event_type="skill.execution_failed",
                    payload={
                        "capability_id": request.capability_id,
                        "skill_name": resolved.manifest.name,
                        "mode": execution.mode,
                        "reason": "missing_input",
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
            interrupt = await build_missing_input_interrupt_with_question(
                request=request,
                manifest=resolved.manifest,
                skill_name=resolved.manifest.name,
                entrypoint=script.name,
                missing=script_result.missing,
                resolved_payload=script_result.resolution.payload if script_result.resolution is not None else {},
                sources=script_result.resolution.sources if script_result.resolution is not None else {},
                question_text_generator=self._skill_input_text_generator,
            )
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload={"skill_name": resolved.manifest.name, "missing": list(script_result.missing)},
                events=tuple(events),
                interrupt=interrupt,
                error=CapabilityExecutionError(
                    code="skill_input_missing",
                    message="Missing required skill input.",
                    retriable=False,
                    metadata={"missing": list(script_result.missing)},
                ),
            )

        events.append(
            self._make_event(
                request,
                event_type="skill.entrypoint_started",
                payload={"skill_name": resolved.manifest.name, "entrypoint": script.name},
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )
        if script_result.status == "failed":
            events.append(
                self._make_event(
                    request,
                    event_type="skill.entrypoint_failed",
                    payload={
                        "skill_name": resolved.manifest.name,
                        "entrypoint": script.name,
                        "reason": script_result.failure_reason,
                        "code": script_result.failure_code,
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
            events.append(
                self._make_event(
                    request,
                    event_type="skill.execution_failed",
                    payload={
                        "capability_id": request.capability_id,
                        "skill_name": resolved.manifest.name,
                        "mode": execution.mode,
                        "reason": script_result.failure_code,
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload={"skill_name": resolved.manifest.name},
                events=tuple(events),
                error=CapabilityExecutionError(
                    code=script_result.failure_code,
                    message=script_result.failure_reason or "Skill script failed.",
                    retriable=script_result.failure_code == "skill_script_timeout",
                ),
            )

        artifacts = []
        if script_result.artifact is not None:
            artifacts.append(script_result.artifact)
        for rejection in script_result.rejections:
            events.append(
                self._make_event(
                    request,
                    event_type="skill.output_file_rejected",
                    payload={
                        "skill_name": resolved.manifest.name,
                        "entrypoint": script.name,
                        "path": getattr(rejection, "path", ""),
                        "reason": getattr(rejection, "reason", "output_file_rejected"),
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        if script_result.output_file_count:
            events.append(
                self._make_event(
                    request,
                    event_type="skill.output_file_collected",
                    payload={
                        "skill_name": resolved.manifest.name,
                        "entrypoint": script.name,
                        "file_count": script_result.output_file_count,
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        output_payload = normalize_skill_response_payload(script_result.output)
        script_missing = missing_input_fields_from_payload(output_payload)
        if script_missing:
            events.append(
                self._make_event(
                    request,
                    event_type="skill.input_missing",
                    payload={
                        "skill_name": resolved.manifest.name,
                        "entrypoint": script.name,
                        "missing": list(script_missing),
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
            interrupt = await build_missing_input_interrupt_with_question(
                request=request,
                manifest=resolved.manifest,
                skill_name=resolved.manifest.name,
                entrypoint=script.name,
                missing=script_missing,
                resolved_payload=script_result.resolution.payload if script_result.resolution is not None else {},
                sources=script_result.resolution.sources if script_result.resolution is not None else {},
                question_text_generator=self._skill_input_text_generator,
                runtime_output_payload=output_payload,
            )
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload=output_payload,
                artifacts=tuple(artifacts),
                events=tuple(events),
                interrupt=interrupt,
            )
        response_text = coerce_skill_response_text(output_payload)
        display_artifact_specs = output_payload.pop("display_artifacts", None)
        artifacts.extend(self._make_display_artifacts(request, display_artifact_specs))
        if execution.answer_mode == "direct" and response_text:
            artifacts.append(self._make_text_artifact(request, response_text))
        if output_payload.get("is_error") is True:
            events.append(
                self._make_event(
                    request,
                    event_type="skill.output_error",
                    payload=self._skill_output_error_payload(
                        request=request,
                        resolved=resolved,
                        execution=execution,
                        script_name=script.name,
                        output_payload=output_payload,
                        response_text=response_text,
                    ),
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        output_error = self._validate_output_contract(
            request=request,
            resolved=resolved,
            output_payload=output_payload,
        )
        events.append(output_error[0])
        if output_error[1] is not None:
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload=output_payload,
                artifacts=tuple(artifacts),
                events=tuple(events),
                error=output_error[1],
            )
        events.append(
            self._make_event(
                request,
                event_type="skill.entrypoint_completed",
                payload={"skill_name": resolved.manifest.name, "entrypoint": script.name, "schema_validated": True},
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )
        events.append(
            self._make_event(
                request,
                event_type="skill.execution_completed",
                payload={
                    "capability_id": request.capability_id,
                    "skill_name": resolved.manifest.name,
                    "mode": execution.mode,
                    "answer_mode": execution.answer_mode,
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                },
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output_payload,
            artifacts=tuple(artifacts),
            events=tuple(events),
        )

    def _skill_output_error_payload(
        self,
        *,
        request: CapabilityExecutionRequest,
        resolved: _ResolvedSkill,
        execution: SkillExecutionConfig,
        script_name: str,
        output_payload: Mapping[str, Any],
        response_text: str,
    ) -> dict[str, Any]:
        return {
            "severity": "warning",
            "capability_id": request.capability_id,
            "skill_name": resolved.manifest.name,
            "entrypoint": script_name,
            "answer_mode": execution.answer_mode,
            "error_code": self._safe_output_string(output_payload.get("error_code"), default="skill_output_error"),
            "error_type": self._safe_output_string(output_payload.get("error_type"), default="SkillOutputError"),
            "error_message": self._safe_output_string(
                output_payload.get("error") or output_payload.get("error_message") or response_text,
                default="Skill output indicated failure.",
            ),
            "retriable": bool(output_payload.get("retriable")),
            "stage": self._safe_output_string(output_payload.get("stage"), default="unknown"),
            "status": self._safe_output_string(output_payload.get("status"), default="failed"),
            "output_keys": sorted(str(key) for key in output_payload.keys()),
            "response_text_preview": self._safe_output_string(response_text, default=""),
        }

    @staticmethod
    def _safe_output_string(value: Any, *, default: str, max_length: int = 500) -> str:
        if value is None:
            return default
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
        text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[REDACTED]", text)
        text = re.sub(r"(?i)(token\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", text)
        text = re.sub(r"(?i)(cookie\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", text)
        if not text:
            return default
        return text[:max_length]

    def _make_display_artifacts(self, request: CapabilityExecutionRequest, specs: Any) -> tuple[Artifact, ...]:
        if not isinstance(specs, list | tuple):
            return ()
        artifacts: list[Artifact] = []
        for index, raw_spec in enumerate(specs):
            if not isinstance(raw_spec, Mapping):
                continue
            artifact_type = self._display_artifact_type(raw_spec.get("artifact_type"))
            if artifact_type is None:
                continue
            storage_ref = self._display_artifact_storage_ref(raw_spec)
            if not storage_ref:
                continue
            suffix = self._artifact_id_suffix(raw_spec.get("artifact_id_suffix") or raw_spec.get("artifact_role"), index)
            digest = hashlib.sha256(f"{request.node_id}:{artifact_type}:{suffix}:{storage_ref}".encode("utf-8")).hexdigest()[:12]
            summary = self._safe_output_string(raw_spec.get("summary"), default="", max_length=120) or None
            artifacts.append(
                Artifact(
                    artifact_id=f"{request.node_id}:skill_display:{digest}:{suffix}",
                    task_id=request.task_id,
                    producer_node_id=request.node_id,
                    artifact_type=artifact_type,
                    storage_ref=storage_ref,
                    summary=summary,
                    is_complete=True,
                )
            )
        return tuple(artifacts)

    @staticmethod
    def _display_artifact_type(value: Any) -> ArtifactType | None:
        raw = str(value or ArtifactType.JSON).strip().lower()
        try:
            artifact_type = ArtifactType(raw)
        except ValueError:
            return None
        if artifact_type == ArtifactType.FILE:
            return None
        return artifact_type

    @staticmethod
    def _display_artifact_storage_ref(spec: Mapping[str, Any]) -> str:
        value = spec.get("storage_ref")
        if value is None:
            value = spec.get("payload")
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _artifact_id_suffix(value: Any, index: int) -> str:
        raw = str(value or f"display_{index}").strip().lower()
        safe = re.sub(r"[^a-z0-9_.-]+", "_", raw).strip("_.-")
        return safe[:48] or f"display_{index}"

    async def _execute_platform_service_skill(
        self,
        *,
        request: CapabilityExecutionRequest,
        resolved: _ResolvedSkill,
        execution,
        user_message: str,
        artifact_context: tuple[Mapping[str, Any], ...],
        started_at: float,
        prior_events: list[EventRecord],
    ) -> CapabilityExecutionResult:
        events = list(prior_events)
        try:
            handler, services = self._platform_handler_registry.resolve(
                capability_id=request.capability_id,
                manifest=resolved.manifest,
                config=execution,
                service_registry=self._service_registry,
                public_skill_roots=resolved.public_skill_roots,
            )
        except PermissionError as exc:
            events.append(
                self._make_event(
                    request,
                    event_type="skill.service_denied",
                    payload={
                        "capability_id": request.capability_id,
                        "skill_name": resolved.manifest.name,
                        "handler": execution.handler,
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
            events.append(
                self._make_event(
                    request,
                    event_type="skill.execution_failed",
                    payload={
                        "capability_id": request.capability_id,
                        "skill_name": resolved.manifest.name,
                        "mode": execution.mode,
                        "reason": "service_denied",
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload={"skill_name": resolved.manifest.name},
                events=tuple(events),
                error=CapabilityExecutionError(
                    code="skill_service_denied",
                    message=str(exc),
                    retriable=False,
                ),
            )

        events.append(
            self._make_event(
                request,
                event_type="skill.service_bound",
                payload={
                    "capability_id": request.capability_id,
                    "skill_name": resolved.manifest.name,
                    "handler": execution.handler,
                    "services": tuple(sorted(services)),
                },
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )
        try:
            handler_result = await call_platform_handler(
                handler,
                SkillPlatformExecutionContext(
                    capability_id=request.capability_id,
                    conversation_id=request.conversation_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    manifest=resolved.manifest,
                    skill_bundle_revision=resolved.revision,
                    input_payload={
                        "query": user_message,
                        "user_message": user_message,
                        **{key: value for key, value in request.input_payload.items() if key not in {"user_message", "query"}},
                    },
                    artifact_context=artifact_context,
                    dependency_outputs=request.dependency_outputs,
                    safe_metadata=build_skill_safe_metadata(request.metadata),
                    services=services,
                ),
            )
        except Exception as exc:
            events.append(
                self._make_event(
                    request,
                    event_type="skill.execution_failed",
                    payload={
                        "capability_id": request.capability_id,
                        "skill_name": resolved.manifest.name,
                        "mode": execution.mode,
                        "reason": "service_failed",
                        "error_type": type(exc).__name__,
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload={"skill_name": resolved.manifest.name},
                events=tuple(events),
                error=CapabilityExecutionError(
                    code="skill_service_failed",
                    message="Platform skill handler failed.",
                    retriable=False,
                    metadata={"error_type": type(exc).__name__},
                ),
            )

        artifacts = list(handler_result.artifacts)
        output_payload = normalize_skill_response_payload(handler_result.output_payload)
        handler_interrupt = handler_result.interrupt
        if handler_result.error is not None and handler_result.error.code == "skill_input_missing" and handler_interrupt is None:
            missing = handler_result.error.metadata.get("missing") if isinstance(handler_result.error.metadata, Mapping) else None
            if not missing:
                missing = tuple(name for name, spec in resolved.manifest.parameters.items() if spec.required)
            handler_interrupt = await build_missing_input_interrupt_with_question(
                request=request,
                manifest=resolved.manifest,
                skill_name=resolved.manifest.name,
                entrypoint=execution.handler or execution.handler_module or "platform_service",
                missing=missing,
                question_text_generator=self._skill_input_text_generator,
                runtime_output_payload=output_payload,
            )
            if handler_interrupt is not None:
                missing_fields = tuple(
                    field
                    for field in handler_interrupt.required_fields.keys()
                    if not str(field).startswith("_")
                )
                events.append(
                    self._make_event(
                        request,
                        event_type="skill.input_missing",
                        payload={
                            "skill_name": resolved.manifest.name,
                            "entrypoint": execution.handler or execution.handler_module or "platform_service",
                            "missing": list(missing_fields),
                        },
                        visibility=EventVisibility.AUDIT_ONLY,
                    )
                )
        response_text = coerce_skill_response_text(output_payload)
        if execution.answer_mode == "direct" and response_text:
            artifacts.append(self._make_text_artifact(request, response_text))
        events.extend(handler_result.events)
        if handler_interrupt is None:
            output_error = self._validate_output_contract(
                request=request,
                resolved=resolved,
                output_payload=output_payload,
            )
            events.append(output_error[0])
            if output_error[1] is not None:
                return CapabilityExecutionResult(
                    capability_id=request.capability_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    output_payload=output_payload,
                    artifacts=tuple(artifacts),
                    events=tuple(events),
                    error=output_error[1],
                )
        if handler_result.error is None and handler_interrupt is None:
            events.append(
                self._make_event(
                    request,
                    event_type="skill.execution_completed",
                    payload={
                        "capability_id": request.capability_id,
                        "skill_name": resolved.manifest.name,
                        "mode": execution.mode,
                        "answer_mode": execution.answer_mode,
                        "duration_ms": int((time.monotonic() - started_at) * 1000),
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        elif handler_interrupt is not None:
            events.append(
                self._make_event(
                    request,
                    event_type="skill.execution_interrupted",
                    payload={
                        "capability_id": request.capability_id,
                        "skill_name": resolved.manifest.name,
                        "mode": execution.mode,
                        "interrupt_id": handler_interrupt.interrupt_id,
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        else:
            events.append(
                self._make_event(
                    request,
                    event_type="skill.execution_failed",
                    payload={
                        "capability_id": request.capability_id,
                        "skill_name": resolved.manifest.name,
                        "mode": execution.mode,
                        "reason": handler_result.error.code,
                    },
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output_payload,
            artifacts=tuple(artifacts),
            events=tuple(events),
            interrupt=handler_interrupt,
            error=handler_result.error,
        )

    def _validate_output_contract(
        self,
        *,
        request: CapabilityExecutionRequest,
        resolved: _ResolvedSkill,
        output_payload: Mapping[str, Any],
    ) -> tuple[EventRecord, CapabilityExecutionError | None]:
        contract = resolved.manifest.contract
        if contract is None or not contract.outputs:
            return (
                self._make_event(
                    request,
                    event_type="skill.output_contract_validated",
                    payload={"skill_name": resolved.manifest.name, "schema_validated": True, "contract": "legacy"},
                    visibility=EventVisibility.AUDIT_ONLY,
                ),
                None,
            )
        output_contract = next(iter(contract.outputs.values()))
        missing = [key for key in output_contract.required if key not in output_payload]
        ok = not missing
        event = self._make_event(
            request,
            event_type="skill.output_contract_validated",
            payload={
                "capability_id": request.capability_id,
                "skill_name": resolved.manifest.name,
                "output_id": output_contract.output_id,
                "schema_validated": ok,
                "missing": missing,
            },
            visibility=EventVisibility.AUDIT_ONLY,
        )
        if ok:
            return event, None
        return event, CapabilityExecutionError(
            code="skill_output_contract_invalid",
            message="Skill output did not satisfy output contract.",
            retriable=False,
            metadata={"missing": missing},
        )

    @staticmethod
    def _resolve_user_message(request: CapabilityExecutionRequest) -> str:
        user_message = str(request.input_payload.get("user_message") or request.input_payload.get("query") or "").strip()
        return user_message

    @staticmethod
    def _error_result(request: CapabilityExecutionRequest, *, code: str, message: str) -> CapabilityExecutionResult:
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload={},
            error=CapabilityExecutionError(code=code, message=message, retriable=False),
        )

    @staticmethod
    def _make_event(
        request: CapabilityExecutionRequest,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        visibility: EventVisibility,
    ) -> EventRecord:
        serialized = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(f"{request.node_id}:{event_type}:{serialized}".encode("utf-8")).hexdigest()[:12]
        return EventRecord(
            event_id=f"{request.node_id}:{event_type}:{digest}",
            conversation_id=request.conversation_id,
            task_id=request.task_id,
            node_id=request.node_id,
            event_type=event_type,
            payload=dict(payload),
            visibility=visibility,
        )

    @staticmethod
    def _make_text_artifact(request: CapabilityExecutionRequest, text: str) -> Artifact:
        digest = hashlib.sha256(f"{request.node_id}:text:{text}".encode("utf-8")).hexdigest()[:12]
        return Artifact(
            artifact_id=f"{request.node_id}:skill_text:{digest}",
            task_id=request.task_id,
            producer_node_id=request.node_id,
            artifact_type=ArtifactType.TEXT,
            storage_ref=text,
            summary=text[:120] or None,
            is_complete=True,
        )

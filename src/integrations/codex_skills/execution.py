from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from src.core.contracts import CapabilityExecutionError
from src.core.models import Artifact, Interrupt

from .input_resolution import SkillInputResolutionContext, SkillInputResolutionResult, SkillInputTextGenerator, resolve_skill_inputs_with_llm
from .internal_keys import SKILL_OUTPUT_ARTIFACT_INTERNAL_KEY, SKILL_OUTPUT_REJECTIONS_INTERNAL_KEY
from .manifest import SkillManifest
from .script_manifest import SkillScriptEntrypoint
from .script_runner import SkillScriptError, SkillScriptRunner

_EXECUTION_MODES = frozenset({"delegated_main_agent", "python_subprocess", "platform_service"})
_ANSWER_MODES = frozenset({"direct", "requires_finalizer", "none"})
_SAFE_ARTIFACT_KEYS = frozenset({"artifact_id", "upload_id", "filename", "mime_type", "content_type", "size", "row_count", "columns", "preview", "summary"})
_BLOCKED_METADATA_KEYS = frozenset({"conversation_memory", "memory_context", "recent_messages", "history_summary", "resolved_user_message"})


class SkillExecutionConfigError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class SkillExecutionConfig:
    mode: str
    answer_mode: str
    trust_scope: str = ""
    services: tuple[str, ...] = ()
    handler: str = ""
    explicit_answer_mode: bool = False


@dataclass(slots=True, frozen=True)
class SkillScriptExecutionResult:
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    resolution: SkillInputResolutionResult | None = None
    missing: tuple[str, ...] = ()
    artifact: Artifact | None = None
    rejections: tuple[Any, ...] = ()
    output_file_count: int = 0
    failure_reason: str = ""
    failure_code: str = "skill_script_failed"


@dataclass(slots=True, frozen=True)
class SkillPlatformHandlerResult:
    output_payload: Mapping[str, Any] = field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()
    events: tuple[Any, ...] = ()
    interrupt: Interrupt | None = None
    error: CapabilityExecutionError | None = None


@dataclass(slots=True, frozen=True)
class SkillPlatformExecutionContext:
    capability_id: str
    conversation_id: str
    task_id: str
    node_id: str
    manifest: SkillManifest
    skill_bundle_revision: str | None
    input_payload: Mapping[str, Any]
    artifact_context: tuple[Mapping[str, Any], ...]
    dependency_outputs: Mapping[str, Mapping[str, Any]]
    safe_metadata: Mapping[str, Any]
    services: Mapping[str, Any]


SkillPlatformHandler = Callable[[SkillPlatformExecutionContext], SkillPlatformHandlerResult | Mapping[str, Any] | Awaitable[SkillPlatformHandlerResult | Mapping[str, Any]]]


class SkillServiceRegistry:
    def __init__(self, services: Mapping[str, Any] | None = None) -> None:
        self._services = dict(services or {})

    def get(self, name: str) -> Any:
        return self._services[name]

    def bind(self, service_names: tuple[str, ...]) -> dict[str, Any]:
        return {name: self.get(name) for name in service_names}


class SkillPlatformHandlerRegistry:
    def __init__(
        self,
        *,
        handlers: Mapping[str, SkillPlatformHandler] | None = None,
        trusted_skill_handlers: Mapping[str, str] | None = None,
        trusted_skill_services: Mapping[str, tuple[str, ...] | list[str] | set[str]] | None = None,
    ) -> None:
        self._handlers = dict(handlers or {})
        self._trusted_skill_handlers = {str(key): str(value) for key, value in dict(trusted_skill_handlers or {}).items()}
        self._trusted_skill_services = {
            str(key): tuple(str(item) for item in value)
            for key, value in dict(trusted_skill_services or {}).items()
        }

    def resolve(self, *, capability_id: str, config: SkillExecutionConfig, service_registry: SkillServiceRegistry) -> tuple[SkillPlatformHandler, dict[str, Any]]:
        expected_handler = self._trusted_skill_handlers.get(capability_id, "")
        if not expected_handler or expected_handler != config.handler:
            raise PermissionError("platform handler is not allowlisted for this skill capability")
        handler = self._handlers.get(config.handler)
        if handler is None:
            raise PermissionError("platform handler is not registered")
        allowed_services = set(self._trusted_skill_services.get(capability_id, ()))
        requested_services = set(config.services)
        if not requested_services.issubset(allowed_services):
            raise PermissionError("requested services are not allowlisted for this skill capability")
        try:
            return handler, service_registry.bind(tuple(sorted(requested_services)))
        except KeyError as exc:
            raise PermissionError("requested services are not registered in runtime") from exc


class SkillScriptExecutionService:
    def __init__(
        self,
        *,
        script_runner: SkillScriptRunner,
        skill_input_text_generator: SkillInputTextGenerator | None = None,
    ) -> None:
        self._script_runner = script_runner
        self._skill_input_text_generator = skill_input_text_generator

    async def execute(
        self,
        *,
        manifest: SkillManifest,
        script: SkillScriptEntrypoint,
        user_message: str,
        metadata: Mapping[str, Any],
        artifact_context: tuple[Mapping[str, Any], ...],
        output_context: Mapping[str, Any],
    ) -> SkillScriptExecutionResult:
        base_payload = {
            "query": user_message,
            "uploaded_artifacts": list(artifact_context),
            "metadata": build_skill_safe_metadata(metadata),
        }
        resolution = await resolve_skill_inputs_with_llm(
            manifest,
            script,
            base_payload,
            SkillInputResolutionContext.from_metadata(
                query=user_message,
                metadata=metadata,
                artifact_summaries=artifact_context,
            ),
            text_generator=self._skill_input_text_generator,
        )
        missing = resolution.missing
        if missing:
            return SkillScriptExecutionResult(
                status="missing_input",
                resolution=resolution,
                missing=missing,
            )
        contract_missing = tuple(key for key in script.input_contract.required if key not in resolution.payload)
        if contract_missing:
            return SkillScriptExecutionResult(
                status="missing_input",
                resolution=resolution,
                missing=contract_missing,
            )
        try:
            output = await self._script_runner.run(
                manifest,
                script,
                resolution.payload,
                output_context=output_context,
            )
        except SkillScriptError as exc:
            return SkillScriptExecutionResult(
                status="failed",
                resolution=resolution,
                failure_reason=str(exc)[:200],
                failure_code=getattr(exc, "code", "skill_script_failed"),
            )
        artifact = output.pop(SKILL_OUTPUT_ARTIFACT_INTERNAL_KEY, None)
        rejections = tuple(output.pop(SKILL_OUTPUT_REJECTIONS_INTERNAL_KEY, ()))
        output_file_count = len(output.get("output_files") or ()) if isinstance(output.get("output_files"), list | tuple) else 0
        return SkillScriptExecutionResult(
            status="completed",
            output=dict(output),
            resolution=resolution,
            artifact=artifact if isinstance(artifact, Artifact) else None,
            rejections=rejections,
            output_file_count=output_file_count,
        )


def resolve_skill_execution_config(manifest: SkillManifest) -> SkillExecutionConfig:
    execution = manifest.metadata.get("execution") if isinstance(manifest.metadata.get("execution"), Mapping) else {}
    mode = str(execution.get("mode") or "").strip().lower()
    if not mode:
        mode = "python_subprocess" if manifest.scripts else "delegated_main_agent"
    if mode not in _EXECUTION_MODES:
        raise SkillExecutionConfigError(f"Unsupported skill execution mode: {mode}")

    handler = str(execution.get("handler") or "").strip()
    trust_scope = str(execution.get("trust_scope") or "").strip().lower()
    services = _string_tuple(execution.get("services"))

    answer_raw = str(execution.get("answer_mode") or "").strip().lower()
    explicit_answer_mode = bool(answer_raw)
    if answer_raw:
        if answer_raw not in _ANSWER_MODES:
            raise SkillExecutionConfigError(f"Unsupported skill answer mode: {answer_raw}")
        answer_mode = answer_raw
    elif mode == "delegated_main_agent":
        answer_mode = "direct"
    elif mode == "python_subprocess":
        answer_mode = "requires_finalizer"
    else:
        raise SkillExecutionConfigError("platform_service skills must declare execution.answer_mode explicitly")

    if mode == "platform_service" and not handler:
        raise SkillExecutionConfigError("platform_service skills must declare execution.handler")

    return SkillExecutionConfig(
        mode=mode,
        answer_mode=answer_mode,
        trust_scope=trust_scope,
        services=services,
        handler=handler,
        explicit_answer_mode=explicit_answer_mode,
    )


def select_skill_entrypoint(manifest: SkillManifest) -> SkillScriptEntrypoint:
    auto_run_scripts = tuple(script for script in manifest.scripts if script.auto_run)
    if len(auto_run_scripts) == 1:
        return auto_run_scripts[0]
    if len(auto_run_scripts) > 1:
        raise SkillExecutionConfigError("Multiple auto-run scripts are not supported for direct skill capability execution.")
    if len(manifest.scripts) == 1:
        return manifest.scripts[0]
    if not manifest.scripts:
        raise SkillExecutionConfigError("Skill does not declare a script entrypoint.")
    raise SkillExecutionConfigError("Skill declares multiple scripts and no unique auto-run entrypoint.")


def build_skill_artifact_context(metadata: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    raw_items = metadata.get("uploaded_artifacts") or metadata.get("artifacts") or ()
    if not isinstance(raw_items, list | tuple):
        return ()
    sanitized: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        safe = {
            str(key): value
            for key, value in item.items()
            if str(key).lower() in _SAFE_ARTIFACT_KEYS
        }
        if safe:
            sanitized.append(safe)
    return tuple(sanitized)


def build_skill_safe_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in metadata.items()
        if str(key).lower() not in _BLOCKED_METADATA_KEYS
    }


def coerce_skill_response_text(output_payload: Mapping[str, Any]) -> str:
    for key in ("response_text", "answer", "summary"):
        value = output_payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def normalize_platform_handler_result(value: SkillPlatformHandlerResult | Mapping[str, Any]) -> SkillPlatformHandlerResult:
    if isinstance(value, SkillPlatformHandlerResult):
        return value
    if isinstance(value, Mapping):
        return SkillPlatformHandlerResult(output_payload=dict(value))
    raise TypeError("platform handler must return a mapping or SkillPlatformHandlerResult")


async def call_platform_handler(handler: SkillPlatformHandler, context: SkillPlatformExecutionContext) -> SkillPlatformHandlerResult:
    result = handler(context)
    if inspect.isawaitable(result):
        result = await result
    return normalize_platform_handler_result(result)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, list | tuple | set):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()

from __future__ import annotations

import inspect
import importlib.util
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from src.core.contracts import CapabilityExecutionError
from src.core.models import Artifact, Interrupt

from .input_resolution import SkillInputResolutionContext, SkillInputResolutionResult, SkillInputTextGenerator, resolve_skill_inputs_with_llm
from .input_resolution import SkillInputSource
from .input_schema import SkillInputField, SkillInputSchema, load_input_schemas_for_contract, validate_selected_schema_payload
from .input_schema_selector import select_input_schema
from .missing_input_interrupt import SLOT_COLLECTION_METADATA_KEY
from .slot_state import schema_from_snapshot
from .internal_keys import SKILL_OUTPUT_ARTIFACT_INTERNAL_KEY, SKILL_OUTPUT_REJECTIONS_INTERNAL_KEY
from .manifest import SkillManifest
from .rust_contract import load_skill_runtime_contract
from .rust_contract import contract_mapping as skill_runtime_contract_mapping
from .rust_contract import status_list as skill_runtime_status_list
from .script_manifest import SkillScriptEntrypoint
from .script_runner import SkillScriptError, SkillScriptRunner

_SAFE_ARTIFACT_KEYS = frozenset({
    "artifact_id",
    "upload_id",
    "filename",
    "original_filename",
    "normalized_filename",
    "mime_type",
    "content_type",
    "normalized_content_type",
    "file_type",
    "size",
    "size_bytes",
    "sha256",
    "row_count",
    "columns",
    "preview",
    "summary",
    "selected_sheet",
    "requires_sheet_selection",
})
_SCRIPT_ARTIFACT_KEYS = frozenset((*_SAFE_ARTIFACT_KEYS, "content", "content_base64", "encoding", "storage_key", "conversation_id"))
_BLOCKED_METADATA_KEYS = frozenset(
    {
        "conversation_memory",
        "history_summary",
        "memory_context",
        "recent_messages",
        "resolved_user_message",
        "skill_slot_collection",
    }
)


class SkillExecutionConfigError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class SkillExecutionConfig:
    mode: str
    answer_mode: str
    trust_scope: str = ""
    services: tuple[str, ...] = ()
    handler: str = ""
    handler_module: str = ""
    handler_factory: str = "build_handler"
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
        public_skill_roots: tuple[str | Path, ...] | list[str | Path] | set[str | Path] = (),
        rust_policy_client: Any | None = None,
        rust_policy_mode: str | None = None,
        rust_policy_shadow_diff_sink: Callable[[Mapping[str, str]], None] | None = None,
    ) -> None:
        self._handlers = dict(handlers or {})
        self._trusted_skill_handlers = {str(key): str(value) for key, value in dict(trusted_skill_handlers or {}).items()}
        self._trusted_skill_services = {
            str(key): tuple(str(item) for item in value)
            for key, value in dict(trusted_skill_services or {}).items()
        }
        self._public_skill_roots = tuple(_resolve_path(root) for root in public_skill_roots)
        self._loaded_project_handlers: dict[tuple[str, str, str, int, int], SkillPlatformHandler] = {}
        self._rust_policy_client = rust_policy_client
        self._rust_policy_mode = _resolve_rust_policy_mode(rust_policy_mode)
        self._rust_policy_shadow_diff_sink = rust_policy_shadow_diff_sink
        self.rust_policy_shadow_diffs: list[dict[str, str]] = []

    def resolve(
        self,
        *,
        capability_id: str,
        manifest: SkillManifest | None = None,
        config: SkillExecutionConfig,
        service_registry: SkillServiceRegistry,
        public_skill_roots: tuple[Path, ...] = (),
    ) -> tuple[SkillPlatformHandler, dict[str, Any]]:
        rust_policy_response = self._validate_rust_policy(
            capability_id=capability_id,
            manifest=manifest,
            config=config,
        )
        try:
            expected_handler = self._trusted_skill_handlers.get(capability_id, "")
            if expected_handler:
                if expected_handler != config.handler:
                    raise PermissionError("platform handler is not allowlisted for this skill capability")
                handler = self._handlers.get(config.handler)
                if handler is None:
                    raise PermissionError("platform handler is not registered")
                allowed_services = set(self._trusted_skill_services.get(capability_id, ()))
            elif manifest is not None and config.trust_scope == "project":
                handler = self._load_project_handler(
                    manifest=manifest,
                    config=config,
                    public_skill_roots=public_skill_roots,
                )
                allowed_services = set(config.services)
            else:
                raise PermissionError("platform handler is not allowlisted for this skill capability")
            requested_services = set(config.services)
            if not requested_services.issubset(allowed_services):
                raise PermissionError("requested services are not allowlisted for this skill capability")
            services = service_registry.bind(tuple(sorted(requested_services)))
        except (KeyError, PermissionError) as exc:
            self._record_rust_policy_shadow_diff(
                capability_id=capability_id,
                manifest=manifest,
                response=rust_policy_response,
                legacy_allowed=False,
            )
            if isinstance(exc, KeyError):
                raise PermissionError("requested services are not registered in runtime") from exc
            raise
        self._record_rust_policy_shadow_diff(
            capability_id=capability_id,
            manifest=manifest,
            response=rust_policy_response,
            legacy_allowed=True,
        )
        return handler, services

    def _validate_rust_policy(
        self,
        *,
        capability_id: str,
        manifest: SkillManifest | None,
        config: SkillExecutionConfig,
    ) -> dict[str, Any] | None:
        if self._rust_policy_mode == "off":
            return None
        if self._rust_policy_client is None:
            if self._rust_policy_mode == "enforce":
                raise PermissionError("Rust Skill Runtime policy client is required in enforce mode")
            return None
        policy_request = {
            "skill_name": manifest.name if manifest is not None else capability_id,
            "capability_id": capability_id,
            "execution_mode": config.mode,
            "trust_scope": config.trust_scope,
            "handler": config.handler,
            "manifest_services": config.services,
            "runtime_allowlist_services": self._trusted_skill_services.get(capability_id, ()),
            "requested_services": config.services,
            "runtime_allowlist_handlers": _runtime_allowlist_handlers(
                self._trusted_skill_handlers,
                capability_id,
            ),
            "x_runtime_rust": _skill_owned_rust_metadata(manifest),
        }
        started_at = time.perf_counter()
        try:
            response = self._rust_policy_client.validate_policy(**policy_request)
        except Exception as exc:  # noqa: BLE001 - policy client failures are fail-closed in enforce, audit-only in shadow.
            if self._rust_policy_mode == "enforce":
                raise PermissionError(f"Rust Skill Runtime policy validation failed: {exc}") from exc
            return {
                "allowed": False,
                "bundle_fingerprint": "",
                "error": {"code": "skill_runtime_policy_unavailable", "message": str(exc)},
                "_duration_ms": _elapsed_ms(started_at),
                "_input_fingerprint": _fingerprint(policy_request),
            }
        response = dict(response)
        response["_duration_ms"] = _elapsed_ms(started_at)
        response["_input_fingerprint"] = _fingerprint(policy_request)
        if self._rust_policy_mode == "enforce":
            _raise_if_rust_policy_denied(response)
        return response

    def _record_rust_policy_shadow_diff(
        self,
        *,
        capability_id: str,
        manifest: SkillManifest | None,
        response: Mapping[str, Any] | None,
        legacy_allowed: bool,
    ) -> None:
        if self._rust_policy_mode != "shadow" or response is None:
            return
        rust_allowed = bool(response.get("allowed"))
        if rust_allowed == legacy_allowed:
            return
        error = response.get("error") if isinstance(response.get("error"), Mapping) else {}
        event = {
            "component": "maf_skill_runtime",
            "capability_id": capability_id,
            "skill_name": manifest.name if manifest is not None else capability_id,
            "legacy_allowed": str(legacy_allowed).lower(),
            "rust_allowed": str(rust_allowed).lower(),
            "error_code": str(error.get("code") or ""),
            "input_fingerprint": str(response.get("_input_fingerprint") or ""),
            "legacy_output_fingerprint": _fingerprint({"allowed": legacy_allowed}),
            "rust_output_fingerprint": _fingerprint(
                {"allowed": rust_allowed, "error_code": str(error.get("code") or "")}
            ),
            "duration_ms": str(response.get("_duration_ms") or "0"),
        }
        self.rust_policy_shadow_diffs.append(event)
        if self._rust_policy_shadow_diff_sink is not None:
            try:
                self._rust_policy_shadow_diff_sink(event)
            except Exception:
                # Shadow compare is audit-only by contract: audit/metrics sink
                # failures must never change the legacy user-visible result.
                pass

    @staticmethod
    def _bind_services(config: SkillExecutionConfig, service_registry: SkillServiceRegistry) -> dict[str, Any]:
        try:
            return service_registry.bind(tuple(sorted(set(config.services))))
        except KeyError as exc:
            raise PermissionError("requested services are not registered in runtime") from exc

    def _load_project_handler(
        self,
        *,
        manifest: SkillManifest,
        config: SkillExecutionConfig,
        public_skill_roots: tuple[Path, ...],
    ) -> SkillPlatformHandler:
        if not self._is_public_project_skill(manifest.source_path, public_skill_roots):
            raise PermissionError("project platform handler skill is outside public skill roots")
        module_path = _safe_relative_file(manifest.root_dir, config.handler_module)
        stat = module_path.stat()
        key = (config.handler, config.handler_factory, str(module_path.resolve()), stat.st_mtime_ns, stat.st_size)
        cached = self._loaded_project_handlers.get(key)
        if cached is not None:
            return cached
        module_name = _project_module_name(manifest.root_dir, module_path)
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise PermissionError("platform handler module cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        runtime_path = manifest.root_dir / "runtime"
        sys_path_values = [str(manifest.root_dir), str(runtime_path)]
        inserted: list[str] = []
        for sys_path_value in reversed(sys_path_values):
            if sys_path_value not in sys.path:
                sys.path.insert(0, sys_path_value)
                inserted.append(sys_path_value)
        previous_module = sys.modules.get(module_name)
        try:
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        finally:
            if previous_module is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous_module
            for sys_path_value in inserted:
                try:
                    sys.path.remove(sys_path_value)
                except ValueError:
                    pass
        factory = getattr(module, config.handler_factory, None)
        if not callable(factory):
            raise PermissionError("platform handler factory is not callable")
        handler = _call_handler_factory(factory, manifest)
        if not callable(handler):
            raise PermissionError("platform handler factory did not return a callable handler")
        self._loaded_project_handlers[key] = handler
        return handler

    def _is_public_project_skill(self, path: Path, public_skill_roots: tuple[Path, ...]) -> bool:
        roots = tuple(_resolve_path(root) for root in public_skill_roots) or self._public_skill_roots
        if not roots:
            return False
        source = _resolve_path(path)
        for root in roots:
            try:
                source.relative_to(root)
                return True
            except ValueError:
                continue
        return False


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
        script_artifact_context: tuple[Mapping[str, Any], ...] | None = None,
        output_context: Mapping[str, Any],
    ) -> SkillScriptExecutionResult:
        prompt_artifact_context = tuple(artifact_context)
        if not prompt_artifact_context and script_artifact_context:
            prompt_artifact_context = _sanitize_artifact_items(
                script_artifact_context,
                allowed_keys=_SAFE_ARTIFACT_KEYS,
            )
        base_payload = {
            "query": user_message,
            "uploaded_artifacts": list(prompt_artifact_context),
            "metadata": build_skill_safe_metadata(metadata),
        }
        context = SkillInputResolutionContext.from_metadata(
            query=user_message,
            metadata=metadata,
            artifact_summaries=prompt_artifact_context,
        )
        if manifest.contract is not None:
            resolution = await self._resolve_v2_inputs(
                manifest=manifest,
                script=script,
                base_payload=base_payload,
                context=context,
            )
        else:
            resolution = await resolve_skill_inputs_with_llm(
                manifest,
                script,
                base_payload,
                context,
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
        runner_payload = {
            **resolution.payload,
            "uploaded_artifacts": list(script_artifact_context or artifact_context),
        }
        try:
            output = await self._script_runner.run(
                manifest,
                script,
                runner_payload,
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

    async def _resolve_v2_inputs(
        self,
        *,
        manifest: SkillManifest,
        script: SkillScriptEntrypoint,
        base_payload: Mapping[str, Any],
        context: SkillInputResolutionContext,
    ) -> SkillInputResolutionResult:
        assert manifest.contract is not None
        try:
            schemas = load_input_schemas_for_contract(manifest.contract)
        except Exception as exc:
            return SkillInputResolutionResult(payload=dict(base_payload), missing=(), diagnostics=(f"schema_load_failed:{type(exc).__name__}",))
        if not manifest.contract.input_schemas:
            payload = dict(base_payload)
            payload["_selected_schema_id"] = ""
            payload["_selected_entrypoint"] = script.name
            return SkillInputResolutionResult(payload=payload, missing=(), sources={})
        active_slot_collection = context.active_slot_collection if isinstance(context.active_slot_collection, Mapping) else None
        if active_slot_collection and active_slot_collection.get("selected_schema_id") and isinstance(active_slot_collection.get("schema_snapshot"), Mapping):
            try:
                schema = schema_from_snapshot(active_slot_collection["schema_snapshot"])
            except Exception as exc:
                return SkillInputResolutionResult(
                    payload=dict(base_payload),
                    missing=("_slot_schema_snapshot",),
                    diagnostics=(f"schema_snapshot_load_failed:{type(exc).__name__}",),
                )
            selected_schema_id = str(active_slot_collection.get("selected_schema_id") or "")
            if schema.schema_id != selected_schema_id:
                return SkillInputResolutionResult(
                    payload=dict(base_payload),
                    missing=("_slot_schema_snapshot",),
                    diagnostics=("schema_snapshot_mismatch",),
                )
            payload = dict(base_payload)
            payload["_selected_schema_id"] = schema.schema_id
            payload["_selected_entrypoint"] = str(active_slot_collection.get("selected_entrypoint") or script.name)
            sources: dict[str, SkillInputSource] = {}
            _resolve_v2_fields_from_slot_collection(schema, payload, sources, active_slot_collection)
            validation = validate_selected_schema_payload(
                schema,
                payload,
                candidate_sources={name: source.source for name, source in sources.items()},
            )
            if validation.invalid:
                payload["_invalid"] = [
                    {"field": issue.field, "reason": issue.reason, "message": issue.message}
                    for issue in validation.invalid
                ]
            diagnostics = tuple(f"invalid:{issue.field}:{issue.reason}" for issue in validation.invalid)
            missing = tuple(dict.fromkeys((*validation.missing, *(issue.field for issue in validation.invalid if issue.field in schema.inputs))))
            return SkillInputResolutionResult(payload=payload, missing=missing, sources=sources, diagnostics=diagnostics)
        selection_metadata = dict(base_payload.get("metadata")) if isinstance(base_payload.get("metadata"), Mapping) else {}
        if active_slot_collection is not None:
            selection_metadata[SLOT_COLLECTION_METADATA_KEY] = active_slot_collection
        selection = select_input_schema(
            manifest.contract,
            schemas,
            query=context.query,
            payload=base_payload,
            metadata=selection_metadata,
            artifact_summaries=context.artifact_summaries,
        )
        payload = dict(base_payload)
        sources: dict[str, SkillInputSource] = {}
        if not selection.selected:
            field = selection.missing_selector_field or "schema"
            payload["_selected_schema_id"] = ""
            payload["_selected_entrypoint"] = script.name
            payload["_schema_selection_reason"] = selection.reason
            return SkillInputResolutionResult(payload=payload, missing=(field,), sources=sources, diagnostics=(f"schema_selector:{selection.reason}",))
        schema = schemas[selection.selected_schema_id]
        payload["_selected_schema_id"] = selection.selected_schema_id
        payload["_selected_entrypoint"] = selection.selected_entrypoint or script.name
        resolved_payload, sources = _resolve_v2_structured_schema_fields(schema, payload, context)
        llm_diagnostics, prompt_profile = await _resolve_v2_schema_fields_with_llm(
            manifest=manifest,
            script=script,
            schema=schema,
            payload=resolved_payload,
            sources=sources,
            context=context,
            text_generator=self._skill_input_text_generator,
            base_payload=base_payload,
        )
        _resolve_v2_text_schema_fields(schema, resolved_payload, sources, context)
        _resolve_v2_fields_from_slot_collection(schema, resolved_payload, sources, context.active_slot_collection)
        validation = validate_selected_schema_payload(
            schema,
            resolved_payload,
            candidate_sources={name: source.source for name, source in sources.items()},
        )
        if validation.invalid:
            resolved_payload["_invalid"] = [
                {"field": issue.field, "reason": issue.reason, "message": issue.message}
                for issue in validation.invalid
            ]
        diagnostics = tuple(dict.fromkeys((*llm_diagnostics, *(f"invalid:{issue.field}:{issue.reason}" for issue in validation.invalid))))
        missing = tuple(dict.fromkeys((*validation.missing, *(issue.field for issue in validation.invalid if issue.field in schema.inputs))))
        return SkillInputResolutionResult(payload=resolved_payload, missing=missing, sources=sources, diagnostics=diagnostics, prompt_profile=prompt_profile)


def resolve_skill_execution_config(manifest: SkillManifest) -> SkillExecutionConfig:
    if manifest.contract is not None:
        contract = manifest.contract
        runtime = contract.runtime
        entrypoint = next(iter(contract.entrypoints.values()), None)
        mode = (entrypoint.runtime if entrypoint is not None else runtime.mode).strip().lower()
        answer_mode = (
            (entrypoint.answer_mode if entrypoint is not None else "")
            or runtime.answer_mode
            or (
                skill_runtime_contract_mapping("default_answer_mode_by_execution_mode").get(mode)
                if mode in skill_runtime_contract_mapping("default_answer_mode_by_execution_mode")
                else ""
            )
        )
        if not answer_mode:
            raise SkillExecutionConfigError("v2 skill contract must declare runtime.answer_mode")
        return SkillExecutionConfig(
            mode=mode,
            answer_mode=answer_mode,
            trust_scope=runtime.trust_scope,
            services=entrypoint.services if entrypoint is not None and entrypoint.services else runtime.services,
            handler=entrypoint.handler if entrypoint is not None and entrypoint.handler else runtime.handler,
            handler_module=entrypoint.handler_module if entrypoint is not None and entrypoint.handler_module else runtime.handler_module,
            handler_factory=entrypoint.handler_factory if entrypoint is not None and entrypoint.handler_factory else runtime.handler_factory,
            explicit_answer_mode=True,
        )
    execution = manifest.metadata.get("execution") if isinstance(manifest.metadata.get("execution"), Mapping) else {}
    mode = str(execution.get("mode") or "").strip().lower()
    if not mode:
        default_execution_modes = skill_runtime_contract_mapping("default_execution_modes")
        mode = default_execution_modes["scripted" if manifest.scripts else "instruction_only"]
    if mode not in skill_runtime_status_list("allowed_execution_modes"):
        raise SkillExecutionConfigError(f"Unsupported skill execution mode: {mode}")

    handler = str(execution.get("handler") or "").strip()
    handler_module = str(execution.get("handler_module") or "").strip()
    handler_factory = str(execution.get("handler_factory") or "build_handler").strip()
    trust_scope = str(execution.get("trust_scope") or "").strip().lower()
    services = _string_tuple(execution.get("services"))

    answer_raw = str(execution.get("answer_mode") or "").strip().lower()
    explicit_answer_mode = bool(answer_raw)
    if answer_raw:
        if answer_raw not in skill_runtime_status_list("allowed_answer_modes"):
            raise SkillExecutionConfigError(f"Unsupported skill answer mode: {answer_raw}")
        answer_mode = answer_raw
    elif mode in skill_runtime_contract_mapping("default_answer_mode_by_execution_mode"):
        answer_mode = skill_runtime_contract_mapping("default_answer_mode_by_execution_mode")[mode]
    else:
        raise SkillExecutionConfigError("platform_service skills must declare execution.answer_mode explicitly")

    if mode == "platform_service" and not handler and not handler_module:
        raise SkillExecutionConfigError("platform_service skills must declare execution.handler or execution.handler_module")

    return SkillExecutionConfig(
        mode=mode,
        answer_mode=answer_mode,
        trust_scope=trust_scope,
        services=services,
        handler=handler,
        handler_module=handler_module,
        handler_factory=handler_factory,
        explicit_answer_mode=explicit_answer_mode,
    )


def select_skill_entrypoint(manifest: SkillManifest) -> SkillScriptEntrypoint:
    if manifest.contract is not None:
        entrypoint = next(iter(manifest.contract.entrypoints.values()), None)
        if entrypoint is None:
            raise SkillExecutionConfigError("Skill contract does not declare an entrypoint.")
        if entrypoint.runtime != "python_subprocess":
            raise SkillExecutionConfigError("Contract entrypoint is not a python_subprocess script.")
        return SkillScriptEntrypoint(
            name=entrypoint.name,
            path=entrypoint.path,
            runtime="python",
            auto_run=True,
            timeout_seconds=entrypoint.timeout_seconds,
        )
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


def _resolve_v2_structured_schema_fields(schema: SkillInputSchema, payload: Mapping[str, Any], context: SkillInputResolutionContext) -> tuple[dict[str, Any], dict[str, SkillInputSource]]:
    resolved = dict(payload)
    sources: dict[str, SkillInputSource] = {}
    artifacts = resolved.get("uploaded_artifacts")
    artifact_count = len(artifacts) if isinstance(artifacts, list | tuple) else len(context.artifact_summaries)
    safe_metadata = resolved.get("metadata") if isinstance(resolved.get("metadata"), Mapping) else {}
    for name, field in schema.inputs.items():
        if name in resolved and resolved[name] not in (None, ""):
            sources[name] = SkillInputSource(source="payload", confidence="high")
            continue
        if name in safe_metadata and safe_metadata[name] not in (None, ""):
            resolved[name] = safe_metadata[name]
            sources[name] = SkillInputSource(source="metadata", confidence="high")
            continue
        if field.const is not None:
            resolved[name] = field.const
            sources[name] = SkillInputSource(source="schema_const", confidence="high")
            continue
        if field.type in {"artifact", "file", "data"}:
            if artifact_count > 0:
                artifact_value: dict[str, Any] = {"available": True, "count": artifact_count}
                source_artifacts = artifacts if isinstance(artifacts, list | tuple) else context.artifact_summaries
                filenames = _artifact_context_filenames(source_artifacts)
                if filenames:
                    artifact_value["filenames"] = list(filenames)
                    if len(filenames) == 1:
                        artifact_value["filename"] = filenames[0]
                resolved[name] = artifact_value
                sources[name] = SkillInputSource(source="artifact", confidence="high")
            continue
    return resolved, sources


def _artifact_context_filenames(items: object) -> tuple[str, ...]:
    if not isinstance(items, list | tuple):
        return ()
    names: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        for key in ("filename", "normalized_filename", "original_filename", "name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                names.append(Path(value.replace("\\", "/")).name)
                break
    return tuple(dict.fromkeys(name for name in names if name))


async def _resolve_v2_schema_fields_with_llm(
    *,
    manifest: SkillManifest,
    script: SkillScriptEntrypoint,
    schema: SkillInputSchema,
    payload: dict[str, Any],
    sources: dict[str, SkillInputSource],
    context: SkillInputResolutionContext,
    text_generator: SkillInputTextGenerator | None,
    base_payload: Mapping[str, Any],
) -> tuple[tuple[str, ...], Mapping[str, Any] | None]:
    if text_generator is None:
        return (), None
    target_fields = {
        name: field
        for name, field in schema.inputs.items()
        if name not in payload and _v2_llm_can_resolve(field)
    }
    if not target_fields:
        return (), None

    prompt_payload = _v2_llm_slot_prompt_payload(
        manifest=manifest,
        script=script,
        schema=schema,
        fields=target_fields,
        payload=payload,
        context=context,
    )
    prompt = (
        "你是一个受限的 v2 Skill 参数补槽器。只根据给定上下文抽取 selected_schema 声明的参数，禁止编造文件、数据、路径或未声明字段。\n"
        "结构化事实已经预先写入 already_resolved；不要覆盖这些字段。对 parameters_to_resolve 中每个字段，如果用户原句、当前消息、解析后消息或近期用户消息中已经给出参数，请抽取 canonical value。\n"
        "artifact/file/data 字段不能由文本伪造；未给出充分证据的字段放入 missing。\n"
        "只返回 JSON 对象，不要 Markdown。格式："
        '{"resolved":{"字段名":{"raw_value":"原文片段","value":规范值,"source":"query|current_user_message|resolved_user_message|recent_user_message"}},'
        '"missing":["字段名"]}\n'
        + json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True)
    )
    prompt_profile: Mapping[str, Any] = {
        "template_id": "v2_skill_input_resolver",
        "schema_id": schema.schema_id,
        "skill_name": manifest.name,
        "entrypoint": script.name,
        "target_fields": sorted(target_fields),
    }
    try:
        from src.orchestration.prompt_profiles import optional_profile_kwargs

        kwargs = optional_profile_kwargs(
            text_generator,
            prompt_profile=prompt_profile,
            metadata=base_payload.get("metadata"),
        )
        raw_response = text_generator(prompt, **kwargs) if kwargs else text_generator(prompt)
        if inspect.isawaitable(raw_response):
            raw_response = await raw_response
        candidates = _parse_v2_llm_slot_candidates(str(raw_response or ""))
    except json.JSONDecodeError:
        return ("v2_llm_invalid_json",), prompt_profile
    except Exception:
        return ("v2_llm_failed",), prompt_profile

    diagnostics: list[str] = []
    for name, candidate in candidates.items():
        field = target_fields.get(name)
        if field is None:
            diagnostics.append("v2_llm_rejected_unknown_field")
            continue
        if name in payload:
            continue
        accepted = _validate_v2_llm_candidate(field, candidate)
        if accepted is None:
            diagnostics.append(f"v2_llm_rejected_invalid_value:{name}")
            continue
        value, source = accepted
        payload[name] = value
        sources[name] = source
    return tuple(dict.fromkeys(diagnostics)), prompt_profile


def _resolve_v2_text_schema_fields(
    schema: SkillInputSchema,
    payload: dict[str, Any],
    sources: dict[str, SkillInputSource],
    context: SkillInputResolutionContext,
) -> None:
    text_sources = (
        ("query", context.query),
        ("current_user_message", context.current_user_message),
        ("resolved_user_message", context.resolved_user_message),
        *(("recent_user_message", item) for item in context.recent_user_messages),
    )
    for name, field in schema.inputs.items():
        if name in payload:
            continue
        if field.type in {"artifact", "file", "data"}:
            continue
        for source_name, text in text_sources:
            if not text:
                continue
            value = _match_v2_field(field, text)
            if value is not None:
                payload[name] = value
                sources[name] = SkillInputSource(source=source_name, confidence="high")
                break


def _v2_llm_can_resolve(field: SkillInputField) -> bool:
    if not field.expose:
        return False
    if field.const is not None:
        return False
    if field.type in {"artifact", "file", "data", "object", "array"}:
        return False
    allowed = tuple(str(item).strip() for item in field.source.allowed if str(item).strip())
    if allowed and not any(source in {"query", "current_user_message", "resolved_user_message", "recent_user_message", "text"} for source in allowed):
        return False
    return field.type in {"string", "integer", "int", "number", "float", "boolean", "bool"}


def _v2_llm_slot_prompt_payload(
    *,
    manifest: SkillManifest,
    script: SkillScriptEntrypoint,
    schema: SkillInputSchema,
    fields: Mapping[str, SkillInputField],
    payload: Mapping[str, Any],
    context: SkillInputResolutionContext,
) -> dict[str, Any]:
    return {
        "skill": {
            "name": manifest.name,
            "description": manifest.description,
            "entrypoint": script.name,
        },
        "selected_schema": {
            "schema_id": schema.schema_id,
            "title": schema.title,
            "description": schema.description,
        },
        "parameters_to_resolve": [
            {
                "name": field.name,
                "type": field.type,
                "required_now": bool(field.required or field.required_when),
                "required_when": dict(field.required_when),
                "aliases": list(field.aliases),
                "patterns": list(field.patterns),
                "enum": list(field.enum),
                "description": field.description,
                "clarification": {
                    "hint": field.clarification.hint,
                    "examples": list(field.clarification.examples),
                },
                "validation": _v2_field_validation_prompt(field),
            }
            for field in fields.values()
        ],
        "already_resolved": _v2_safe_resolved_payload(payload),
        "context": {
            "query": context.query,
            "current_user_message": context.current_user_message,
            "resolved_user_message": context.resolved_user_message,
            "recent_user_messages": list(context.recent_user_messages),
            "artifact_summaries": [_v2_safe_artifact_summary(item) for item in context.artifact_summaries],
        },
    }


def _v2_field_validation_prompt(field: SkillInputField) -> dict[str, Any]:
    validation: dict[str, Any] = {}
    if field.validation.regex:
        validation["regex"] = field.validation.regex
    if field.validation.min is not None:
        validation["min"] = field.validation.min
    if field.validation.max is not None:
        validation["max"] = field.validation.max
    if field.validation.min_length is not None:
        validation["min_length"] = field.validation.min_length
    if field.validation.max_length is not None:
        validation["max_length"] = field.validation.max_length
    return validation


def _v2_safe_resolved_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        key_text = str(key)
        if key_text in {"metadata", "uploaded_artifacts", "skill_artifacts"}:
            continue
        if isinstance(value, str | int | float | bool) or value is None:
            safe[key_text] = value
        elif isinstance(value, Mapping) and value.get("available") is True:
            safe[key_text] = {"available": True, "count": value.get("count")}
    return safe


def _v2_safe_artifact_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, raw_value in value.items():
        key_text = str(key)
        if key_text.lower() not in _SAFE_ARTIFACT_KEYS:
            continue
        if isinstance(raw_value, str | int | float | bool) or raw_value is None:
            safe[key_text] = raw_value
        elif isinstance(raw_value, list | tuple):
            safe[key_text] = [item for item in raw_value if isinstance(item, str | int | float | bool) or item is None][:20]
    return safe


def _parse_v2_llm_slot_candidates(text: str) -> dict[str, dict[str, Any]]:
    parsed = _load_v2_json_object(text)
    resolved = parsed.get("resolved")
    if not isinstance(resolved, Mapping):
        resolved = {
            key: value
            for key, value in parsed.items()
            if key not in {"missing", "diagnostics", "reasoning"}
        }
    candidates: dict[str, dict[str, Any]] = {}
    for raw_name, raw_candidate in resolved.items():
        name = str(raw_name).strip()
        if not name:
            continue
        if isinstance(raw_candidate, Mapping):
            candidate = dict(raw_candidate)
            if "value" not in candidate:
                candidate = {"value": raw_candidate}
        else:
            candidate = {"value": raw_candidate}
        candidates[name] = candidate
    return candidates


def _load_v2_json_object(text: str) -> Mapping[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise json.JSONDecodeError("empty response", text, 0)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end < start:
            raise
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, Mapping):
        raise json.JSONDecodeError("response is not a JSON object", stripped, 0)
    return parsed


def _validate_v2_llm_candidate(
    field: SkillInputField,
    candidate: Mapping[str, Any],
) -> tuple[Any, SkillInputSource] | None:
    source_hint = str(candidate.get("source") or "").strip()
    if source_hint and source_hint not in {"query", "current_user_message", "resolved_user_message", "recent_user_message"}:
        return None
    value = _coerce_v2_value(field, candidate.get("value"))
    if value is None:
        return None
    source = f"llm_slot_resolver:{source_hint}" if source_hint else "llm_slot_resolver"
    return value, SkillInputSource(source=source, confidence="medium")


def _resolve_v2_fields_from_slot_collection(
    schema,
    payload: dict[str, Any],
    sources: dict[str, SkillInputSource],
    active_slot_collection: Mapping[str, Any] | None,
) -> None:
    if not isinstance(active_slot_collection, Mapping):
        return
    selected_schema_id = str(active_slot_collection.get("selected_schema_id") or "").strip()
    if selected_schema_id and selected_schema_id != schema.schema_id:
        return
    resolved = active_slot_collection.get("resolved")
    if not isinstance(resolved, Mapping):
        return
    for name, item in resolved.items():
        field_name = str(name)
        if field_name not in schema.inputs:
            continue
        if not isinstance(item, Mapping):
            value = item
            source = "slot_collection"
        else:
            value = item.get("value", item.get("raw_value"))
            source = str(item.get("source") or "slot_collection")
        if value in (None, ""):
            continue
        payload[field_name] = value
        sources[field_name] = SkillInputSource(source=source, confidence="high")


def _match_v2_field(field, text: str) -> Any | None:
    for pattern in field.patterns:
        try:
            match = re.search(pattern, text, flags=re.IGNORECASE)
        except re.error:
            continue
        if match is None:
            continue
        raw = next((group for group in match.groups() if group not in (None, "")), match.group(0))
        return _coerce_v2_value(field, raw)
    aliases = tuple(dict.fromkeys((field.name, *field.aliases)))
    for alias in aliases:
        if not alias:
            continue
        if field.enum:
            for item in field.enum:
                if item and item.lower() in text.lower():
                    return item
        if field.type in {"integer", "int"}:
            patterns = (
                rf"(?:{re.escape(alias)})\\s*[:：=]?\\s*(\\d+)",
                rf"(\\d+)\\s*(?:个|次|列)?\\s*(?:{re.escape(alias)})",
            )
        else:
            patterns = (rf"(?:{re.escape(alias)})\\s*[:：=]\\s*([^\\s,，。；;]+)",)
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match is not None:
                return _coerce_v2_value(field, match.group(1))
    return None


def _coerce_v2_value(field, value: Any) -> Any | None:
    if field.type in {"integer", "int"}:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if field.type in {"number", "float"}:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if field.type in {"boolean", "bool"}:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "yes", "1"}:
            return True
        if text in {"false", "no", "0"}:
            return False
        return None
    return str(value).strip() or None


def build_skill_artifact_context(metadata: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    raw_items = metadata.get("uploaded_artifacts") or metadata.get("artifacts") or ()
    return _sanitize_artifact_items(raw_items, allowed_keys=_SAFE_ARTIFACT_KEYS)


def build_skill_script_artifact_context(
    metadata: Mapping[str, Any],
    *,
    fallback_artifact_context: tuple[Mapping[str, Any], ...] = (),
) -> tuple[dict[str, Any], ...]:
    """Return script-only upload context, including raw content when explicitly available.

    ``uploaded_artifacts`` is the prompt-safe summary channel.  ``skill_artifacts``
    is the execution-only channel created by the API runtime from the same upload
    records, and may contain raw file content.  Keep this helper separate from
    ``build_skill_artifact_context`` so LLM-facing code cannot accidentally gain
    access to raw upload bytes.
    """

    raw_items = metadata.get("skill_artifacts") or metadata.get("uploaded_artifacts") or metadata.get("artifacts")
    sanitized = _sanitize_artifact_items(raw_items, allowed_keys=_SCRIPT_ARTIFACT_KEYS)
    if sanitized:
        return sanitized
    return tuple(dict(item) for item in fallback_artifact_context if isinstance(item, Mapping))


def _sanitize_artifact_items(raw_items: Any, *, allowed_keys: frozenset[str]) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw_items, list | tuple):
        return ()
    sanitized: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        safe = {
            str(key): value
            for key, value in item.items()
            if str(key).lower() in allowed_keys
        }
        if safe:
            sanitized.append(safe)
    return tuple(sanitized)


def build_skill_safe_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        key_text = str(key)
        normalized = key_text.lower()
        if normalized in _BLOCKED_METADATA_KEYS:
            continue
        if normalized == "skill_artifacts":
            safe[key_text] = list(_sanitize_artifact_items(value, allowed_keys=_SAFE_ARTIFACT_KEYS))
            continue
        if normalized in {"uploaded_artifacts", "artifacts"}:
            safe[key_text] = list(_sanitize_artifact_items(value, allowed_keys=_SAFE_ARTIFACT_KEYS))
            continue
        safe[key_text] = value
    return safe


def coerce_skill_response_text(output_payload: Mapping[str, Any]) -> str:
    for key in ("response_text", "answer", "summary"):
        value = output_payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def normalize_skill_response_payload(output_payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(output_payload)
    response_text = coerce_skill_response_text(normalized)
    existing_response_text = normalized.get("response_text")
    if response_text and not (isinstance(existing_response_text, str) and existing_response_text.strip()):
        normalized["response_text"] = response_text
    if normalized.get("ok") is False:
        normalized["is_error"] = True
    return normalized


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


def _resolve_rust_policy_mode(mode: str | None) -> str:
    contract = load_skill_runtime_contract()
    resolved = str(mode if mode is not None else os.environ.get(str(contract["mode_env"]), "off")).strip().lower()
    if resolved not in skill_runtime_status_list("modes"):
        raise SkillExecutionConfigError(f"Unsupported Rust Skill Runtime policy mode: {resolved}")
    return resolved


def _runtime_allowlist_handlers(trusted_skill_handlers: Mapping[str, str], capability_id: str) -> tuple[str, ...]:
    handler = str(trusted_skill_handlers.get(capability_id, "")).strip()
    return (handler,) if handler else ()


def _skill_owned_rust_metadata(manifest: SkillManifest | None) -> dict[str, str]:
    if manifest is None:
        return {}
    x_runtime = manifest.metadata.get("x_runtime")
    if not isinstance(x_runtime, Mapping):
        return {}
    rust = x_runtime.get("rust")
    if not isinstance(rust, Mapping):
        return {}
    return {str(key): str(value) for key, value in rust.items()}


def _raise_if_rust_policy_denied(response: Mapping[str, Any]) -> None:
    if response.get("allowed") is True and response.get("error") is None:
        return
    error = response.get("error") if isinstance(response.get("error"), Mapping) else {}
    code = str(error.get("code") or "skill_runtime_policy_denied")
    message = str(error.get("message") or "Rust Skill Runtime policy denied platform binding")
    raise PermissionError(f"{code}: {message}")


def _elapsed_ms(started_at: float) -> str:
    return str(max(0, round((time.perf_counter() - started_at) * 1000)))


def _fingerprint(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_path(path: str | Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path).expanduser().absolute()


def _safe_relative_file(root: Path, relative_path: str) -> Path:
    raw = Path(relative_path)
    if not str(relative_path).strip():
        raise PermissionError("platform handler module is not declared")
    if raw.is_absolute() or any(part == ".." for part in raw.parts):
        raise PermissionError("platform handler module must be a relative path inside the skill root")
    resolved_root = _resolve_path(root)
    resolved = _resolve_path(resolved_root / raw)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise PermissionError("platform handler module must stay inside the skill root") from exc
    if not resolved.is_file():
        raise PermissionError("platform handler module is missing")
    if resolved.suffix != ".py":
        raise PermissionError("platform handler module must be a Python file")
    return resolved


def _project_module_name(root: Path, module_path: Path) -> str:
    try:
        relative = module_path.resolve().relative_to((root / "runtime").resolve())
    except ValueError:
        try:
            relative = module_path.resolve().relative_to(root.resolve())
        except ValueError:
            return f"_maf_skill_platform_{abs(hash(str(module_path)))}"
    parts = list(relative.with_suffix("").parts)
    if not parts:
        return f"_maf_skill_platform_{abs(hash(str(module_path)))}"
    return ".".join(parts)


def _call_handler_factory(factory: Callable[..., Any], manifest: SkillManifest) -> SkillPlatformHandler:
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return factory()
    required_parameters = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.default is inspect.Signature.empty
        and parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
    ]
    if not required_parameters:
        return factory()
    if len(required_parameters) == 1:
        name = required_parameters[0].name
        if required_parameters[0].kind is inspect.Parameter.KEYWORD_ONLY:
            return factory(**{name: manifest})
        return factory(manifest)
    raise PermissionError("platform handler factory must accept zero arguments or one manifest argument")

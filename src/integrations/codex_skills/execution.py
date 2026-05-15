from __future__ import annotations

import inspect
import importlib.util
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from src.core.contracts import CapabilityExecutionError
from src.core.models import Artifact, Interrupt

from .input_resolution import SkillInputResolutionContext, SkillInputResolutionResult, SkillInputTextGenerator, resolve_skill_inputs_with_llm
from .internal_keys import SKILL_OUTPUT_ARTIFACT_INTERNAL_KEY, SKILL_OUTPUT_REJECTIONS_INTERNAL_KEY
from .manifest import SkillManifest
from .rust_contract import load_skill_runtime_contract
from .rust_contract import contract_mapping as skill_runtime_contract_mapping
from .rust_contract import status_list as skill_runtime_status_list
from .script_manifest import SkillScriptEntrypoint
from .script_runner import SkillScriptError, SkillScriptRunner

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

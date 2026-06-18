from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml


class SkillContractParseError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class SkillContractDiagnostic:
    skill_name: str
    reason: str
    message: str
    source_path_summary: str = ""


@dataclass(slots=True, frozen=True)
class SkillCapabilityContract:
    id: str
    display_name: str
    description: str = ""
    version: str = "1"


@dataclass(slots=True, frozen=True)
class SkillRoutingContract:
    triggers: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    intent_aliases: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class SkillRuntimeContract:
    mode: str
    answer_mode: str = ""
    trust_scope: str = ""
    services: tuple[str, ...] = ()
    handler: str = ""
    handler_module: str = ""
    handler_factory: str = "build_handler"


@dataclass(slots=True, frozen=True)
class SkillEntrypointContract:
    name: str
    runtime: str = "python"
    path: str = ""
    timeout_seconds: float = 10.0
    input_schema: str = ""
    output: str = ""
    handler: str = ""
    handler_module: str = ""
    handler_factory: str = "build_handler"
    services: tuple[str, ...] = ()
    answer_mode: str = ""


@dataclass(slots=True, frozen=True)
class SkillInputSchemaRef:
    schema_id: str
    path: str
    title: str = ""
    description: str = ""
    entrypoint: str = ""
    aliases: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class SkillSchemaSelectorContract:
    strategy: str = "single_schema"
    selector_field: str = ""
    min_confidence: float = 0.75


@dataclass(slots=True, frozen=True)
class SkillFileIntent:
    requires_file: bool = False
    default_allow_multiple: bool = False
    supported_file_types: tuple[str, ...] = ()
    description: str = ""


@dataclass(slots=True, frozen=True)
class SkillOutputContract:
    output_id: str
    required: tuple[str, ...] = ()
    artifacts: tuple[Mapping[str, Any], ...] = ()
    schema: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class SkillResourceRef:
    resource_id: str
    path: str
    title: str = ""
    description: str = ""
    audience: tuple[str, ...] = ("main_agent",)


@dataclass(slots=True, frozen=True)
class SkillResourcePolicy:
    default_audience: tuple[str, ...] = ("main_agent",)
    max_bytes: int = 65536
    deny_paths: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class SkillContract:
    contract_version: str
    capability: SkillCapabilityContract
    runtime: SkillRuntimeContract
    entrypoints: Mapping[str, SkillEntrypointContract]
    input_schemas: Mapping[str, SkillInputSchemaRef] = field(default_factory=dict)
    schema_selector: SkillSchemaSelectorContract = SkillSchemaSelectorContract()
    outputs: Mapping[str, SkillOutputContract] = field(default_factory=dict)
    resources: Mapping[str, SkillResourceRef] = field(default_factory=dict)
    resource_policy: SkillResourcePolicy = SkillResourcePolicy()
    routing: SkillRoutingContract = SkillRoutingContract()
    file_intent: SkillFileIntent = SkillFileIntent()
    source_path: Path = Path("skill.contract.yaml")

    @property
    def root_dir(self) -> Path:
        return self.source_path.parent


_ALLOWED_RUNTIME_MODES = {"python_subprocess", "platform_service", "delegated_main_agent"}
_ALLOWED_SCHEMA_SELECTOR_STRATEGIES = {"single_schema", "deterministic_then_llm"}
_FORBIDDEN_V1_FIELDS = {"auto_run", "run_by_default", "parameters", "input_parameters", "scripts", "execution", "public_usage"}


def parse_skill_contract_file(path: str | Path) -> SkillContract:
    source_path = Path(path)
    try:
        raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SkillContractParseError(f"Invalid skill.contract.yaml: {source_path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise SkillContractParseError(f"Skill contract must be a mapping: {source_path}")
    root = source_path.parent
    contract_version = _required_string(raw, "contract_version", source_path)
    capability = _parse_capability(_required_mapping(raw, "capability", source_path), source_path)
    runtime = _parse_runtime(_mapping(raw.get("runtime")), source_path)
    entrypoints = _parse_entrypoints(raw.get("entrypoints"), runtime, root, source_path)
    input_schemas = _parse_input_schemas(raw.get("input_schemas"), entrypoints, root, source_path)
    outputs = _parse_outputs(raw.get("outputs") or raw.get("output_contracts"), entrypoints, source_path)
    _validate_entrypoint_refs(entrypoints, input_schemas, outputs, source_path)
    selector = _parse_schema_selector(_mapping(raw.get("schema_selector")), source_path)
    resources = _parse_resources(raw.get("resources"), root, source_path)
    resource_policy = _parse_resource_policy(_mapping(raw.get("resource_policy")))
    routing = _parse_routing(_mapping(raw.get("routing")))
    file_intent = _parse_file_intent(_mapping(raw.get("file_intent")), source_path)
    return SkillContract(
        contract_version=contract_version,
        capability=capability,
        runtime=runtime,
        entrypoints=entrypoints,
        input_schemas=input_schemas,
        schema_selector=selector,
        outputs=outputs,
        resources=resources,
        resource_policy=resource_policy,
        routing=routing,
        file_intent=file_intent,
        source_path=source_path,
    )


def forbidden_v1_fields(frontmatter: Mapping[str, Any]) -> tuple[str, ...]:
    present = {str(key) for key in frontmatter if str(key) in _FORBIDDEN_V1_FIELDS}
    scripts = frontmatter.get("scripts")
    if isinstance(scripts, list | tuple):
        for script in scripts:
            if isinstance(script, Mapping):
                if script.get("auto_run") is not None:
                    present.add("auto_run")
                if script.get("run_by_default") is not None:
                    present.add("run_by_default")
    return tuple(sorted(present))


def load_skill_frontmatter(path: str | Path) -> Mapping[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            data = yaml.safe_load("\n".join(lines[1:index])) or {}
            return data if isinstance(data, Mapping) else {}
    return {}


def _parse_capability(value: Mapping[str, Any], source_path: Path) -> SkillCapabilityContract:
    cap_id = _required_string(value, "id", source_path)
    display_name = _required_string(value, "display_name", source_path)
    description = str(value.get("description") or "").strip()
    version = str(value.get("version") or "1").strip() or "1"
    return SkillCapabilityContract(id=cap_id, display_name=display_name, description=description, version=version)


def _parse_runtime(value: Mapping[str, Any], source_path: Path) -> SkillRuntimeContract:
    mode = str(value.get("mode") or "python_subprocess").strip().lower()
    if mode not in _ALLOWED_RUNTIME_MODES:
        raise SkillContractParseError(f"Unsupported skill runtime mode: {mode}: {source_path}")
    return SkillRuntimeContract(
        mode=mode,
        answer_mode=str(value.get("answer_mode") or "").strip().lower(),
        trust_scope=str(value.get("trust_scope") or "").strip().lower(),
        services=_string_tuple(value.get("services")),
        handler=str(value.get("handler") or "").strip(),
        handler_module=str(value.get("handler_module") or "").strip(),
        handler_factory=str(value.get("handler_factory") or "build_handler").strip() or "build_handler",
    )


def _parse_entrypoints(value: Any, runtime: SkillRuntimeContract, root: Path, source_path: Path) -> dict[str, SkillEntrypointContract]:
    entries = _as_named_mappings(value, id_key="name")
    if not entries:
        # Allow compact platform_service contracts that declare handler in runtime.
        if runtime.mode == "platform_service" and (runtime.handler or runtime.handler_module):
            entries = {"default": {"name": "default"}}
        else:
            raise SkillContractParseError(f"Skill contract must declare at least one entrypoint: {source_path}")
    parsed: dict[str, SkillEntrypointContract] = {}
    for name, item in entries.items():
        entry_name = str(item.get("name") or name).strip()
        if not entry_name:
            raise SkillContractParseError(f"Entrypoint name is required: {source_path}")
        mode = str(item.get("runtime") or item.get("mode") or runtime.mode).strip().lower()
        path = str(item.get("path") or item.get("command") or "").strip()
        handler_module = str(item.get("handler_module") or runtime.handler_module).strip()
        if mode == "python_subprocess":
            if not path:
                raise SkillContractParseError(f"python_subprocess entrypoint requires path: {entry_name}: {source_path}")
            _safe_relative(root, path, source_path=source_path, must_exist=False)
        elif mode == "platform_service":
            if not (str(item.get("handler") or runtime.handler).strip() or handler_module):
                raise SkillContractParseError(f"platform_service entrypoint requires handler or handler_module: {entry_name}: {source_path}")
            if handler_module:
                _safe_relative(root, handler_module, source_path=source_path, must_exist=False)
        elif mode != "delegated_main_agent":
            raise SkillContractParseError(f"Unsupported entrypoint runtime: {mode}: {source_path}")
        parsed[entry_name] = SkillEntrypointContract(
            name=entry_name,
            runtime=mode,
            path=path,
            timeout_seconds=float(item.get("timeout_seconds") or item.get("timeout") or 10),
            input_schema=str(item.get("input_schema") or item.get("input_schema_id") or "").strip(),
            output=str(item.get("output") or item.get("output_contract") or item.get("output_id") or "").strip(),
            handler=str(item.get("handler") or runtime.handler).strip(),
            handler_module=handler_module,
            handler_factory=str(item.get("handler_factory") or runtime.handler_factory or "build_handler").strip() or "build_handler",
            services=_string_tuple(item.get("services") or runtime.services),
            answer_mode=str(item.get("answer_mode") or runtime.answer_mode or "").strip().lower(),
        )
    return parsed


def _parse_input_schemas(value: Any, entrypoints: Mapping[str, SkillEntrypointContract], root: Path, source_path: Path) -> dict[str, SkillInputSchemaRef]:
    refs = _as_named_mappings(value, id_key="schema_id")
    parsed: dict[str, SkillInputSchemaRef] = {}
    for schema_id, item in refs.items():
        sid = str(item.get("schema_id") or item.get("id") or schema_id).strip()
        path = str(item.get("path") or "").strip()
        if not sid or not path:
            raise SkillContractParseError(f"Input schema ref requires schema_id and path: {source_path}")
        _safe_relative(root, path, source_path=source_path, must_exist=False)
        entrypoint = str(item.get("entrypoint") or "").strip()
        if entrypoint and entrypoint not in entrypoints:
            raise SkillContractParseError(f"Input schema references unknown entrypoint: {entrypoint}: {source_path}")
        parsed[sid] = SkillInputSchemaRef(
            schema_id=sid,
            path=path,
            title=str(item.get("title") or "").strip(),
            description=str(item.get("description") or "").strip(),
            entrypoint=entrypoint,
            aliases=_string_tuple(item.get("aliases")),
        )
    return parsed


def _parse_outputs(value: Any, entrypoints: Mapping[str, SkillEntrypointContract], source_path: Path) -> dict[str, SkillOutputContract]:
    del entrypoints
    refs = _as_named_mappings(value, id_key="output_id")
    parsed: dict[str, SkillOutputContract] = {}
    for output_id, item in refs.items():
        oid = str(item.get("output_id") or item.get("id") or output_id).strip()
        if not oid:
            raise SkillContractParseError(f"Output contract id is required: {source_path}")
        artifacts = item.get("artifacts")
        if artifacts is None:
            artifacts_tuple: tuple[Mapping[str, Any], ...] = ()
        elif isinstance(artifacts, list | tuple):
            artifacts_tuple = tuple(dict(a) for a in artifacts if isinstance(a, Mapping))
        else:
            raise SkillContractParseError(f"Output artifacts must be a list: {source_path}")
        parsed[oid] = SkillOutputContract(
            output_id=oid,
            required=_string_tuple(item.get("required")),
            artifacts=artifacts_tuple,
            schema=dict(item.get("schema") or {}) if isinstance(item.get("schema"), Mapping) else {},
        )
    return parsed


def _validate_entrypoint_refs(entrypoints: Mapping[str, SkillEntrypointContract], schemas: Mapping[str, SkillInputSchemaRef], outputs: Mapping[str, SkillOutputContract], source_path: Path) -> None:
    for entry in entrypoints.values():
        if entry.input_schema and entry.input_schema not in schemas:
            raise SkillContractParseError(f"Entrypoint references unknown input schema: {entry.input_schema}: {source_path}")
        if entry.output and entry.output not in outputs:
            raise SkillContractParseError(f"Entrypoint references unknown output contract: {entry.output}: {source_path}")


def _parse_schema_selector(value: Mapping[str, Any], source_path: Path) -> SkillSchemaSelectorContract:
    strategy = str(value.get("strategy") or "single_schema").strip().lower()
    if strategy not in _ALLOWED_SCHEMA_SELECTOR_STRATEGIES:
        raise SkillContractParseError(f"Unsupported schema selector strategy: {strategy}: {source_path}")
    return SkillSchemaSelectorContract(
        strategy=strategy,
        selector_field=str(value.get("selector_field") or value.get("missing_field") or "").strip(),
        min_confidence=float(value.get("min_confidence") or 0.75),
    )


def _parse_file_intent(value: Mapping[str, Any], source_path: Path) -> SkillFileIntent:
    del source_path
    if not value:
        return SkillFileIntent()
    return SkillFileIntent(
        requires_file=bool(value.get("requires_file") or value.get("required")),
        default_allow_multiple=bool(value.get("default_allow_multiple") or value.get("allow_multiple")),
        supported_file_types=_string_tuple(value.get("supported_file_types") or value.get("file_types")),
        description=str(value.get("description") or "").strip(),
    )


def _parse_resources(value: Any, root: Path, source_path: Path) -> dict[str, SkillResourceRef]:
    refs = _as_named_mappings(value, id_key="resource_id")
    parsed: dict[str, SkillResourceRef] = {}
    for resource_id, item in refs.items():
        rid = str(item.get("resource_id") or item.get("id") or resource_id).strip()
        path = str(item.get("path") or "").strip()
        if not rid or not path:
            raise SkillContractParseError(f"Resource ref requires resource_id and path: {source_path}")
        _safe_relative(root, path, source_path=source_path, must_exist=False)
        parsed[rid] = SkillResourceRef(
            resource_id=rid,
            path=path,
            title=str(item.get("title") or "").strip(),
            description=str(item.get("description") or "").strip(),
            audience=_string_tuple(item.get("audience")) or ("main_agent",),
        )
    return parsed


def _parse_resource_policy(value: Mapping[str, Any]) -> SkillResourcePolicy:
    return SkillResourcePolicy(
        default_audience=_string_tuple(value.get("default_audience")) or ("main_agent",),
        max_bytes=int(value.get("max_bytes") or value.get("prompt_max_bytes") or 65536),
        deny_paths=_string_tuple(value.get("deny_paths")),
    )


def _parse_routing(value: Mapping[str, Any]) -> SkillRoutingContract:
    return SkillRoutingContract(
        triggers=_string_tuple(value.get("triggers")),
        examples=_string_tuple(value.get("examples")),
        intent_aliases=_string_tuple(value.get("intent_aliases") or value.get("aliases")),
    )


def _as_named_mappings(value: Any, *, id_key: str) -> dict[str, Mapping[str, Any]]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        result: dict[str, Mapping[str, Any]] = {}
        for key, item in value.items():
            if isinstance(item, Mapping):
                result[str(key)] = item
            else:
                raise SkillContractParseError(f"{id_key} entry must be a mapping: {key}")
        return result
    if isinstance(value, list | tuple):
        result = {}
        for item in value:
            if not isinstance(item, Mapping):
                raise SkillContractParseError(f"{id_key} entry must be a mapping")
            key = str(item.get(id_key) or item.get("id") or item.get("name") or "").strip()
            if not key:
                raise SkillContractParseError(f"{id_key} entry is missing an id")
            result[key] = item
        return result
    raise SkillContractParseError(f"{id_key} section must be a mapping or list")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required_mapping(value: Mapping[str, Any], key: str, source_path: Path) -> Mapping[str, Any]:
    child = value.get(key)
    if not isinstance(child, Mapping):
        raise SkillContractParseError(f"Skill contract requires mapping field `{key}`: {source_path}")
    return child


def _required_string(value: Mapping[str, Any], key: str, source_path: Path) -> str:
    text = str(value.get(key) or "").strip()
    if not text:
        raise SkillContractParseError(f"Skill contract requires field `{key}`: {source_path}")
    return text


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list | tuple | set):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _safe_relative(root: Path, relative_path: str, *, source_path: Path, must_exist: bool) -> Path:
    raw = Path(relative_path)
    if raw.is_absolute() or any(part == ".." for part in raw.parts):
        raise SkillContractParseError(f"Contract path must stay inside skill bundle: {relative_path}: {source_path}")
    try:
        resolved_root = root.resolve()
        resolved = (resolved_root / raw).resolve(strict=False)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise SkillContractParseError(f"Contract path must stay inside skill bundle: {relative_path}: {source_path}") from exc
    if must_exist and not resolved.exists():
        raise SkillContractParseError(f"Contract path does not exist: {relative_path}: {source_path}")
    return resolved
